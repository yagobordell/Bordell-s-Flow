from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    InferenceJobResponse,
    ObjectOutput,
)
from ai_video_factory.workers.ideogram4 import (
    IDEOGRAM4_GENERATION_PROFILE,
    IDEOGRAM4_KEYFRAME_TASK,
    IDEOGRAM4_MODEL_ID,
    IDEOGRAM4_REFERENCE_TASK,
    ideogram_application_job_id,
    ideogram_seed_for_job,
)

from .ideogram_caption import validate_ideogram_caption
from .images import GeneratedImage, ImageFormat, ImageQuality
from .inference_jobs import (
    InferenceJobExecutor,
    RemoteInferenceRejectedError,
    cached_inference_response,
)

_SUPPORTED_TASKS = frozenset({IDEOGRAM4_REFERENCE_TASK, IDEOGRAM4_KEYFRAME_TASK})
_SAFETY_BLOCK_DETAIL = "Ideogram 4 safety filter blocked all deterministic caption variants"
_LOCATION_PREFIX = "Canonical location reference."
_RECOVERY_STYLE = {
    "aesthetics": "documentary realism",
    "lighting": "neutral natural daylight with clearly readable form",
    "photo": "realistic reference photography with natural proportions",
    "medium": "documentary photograph",
}
_RECOVERY_BACKGROUND = (
    "Clear reusable environment reference emphasizing stable physical geography, architecture, "
    "materials, layout and recurring landmarks."
)


def build_ideogram_job_request(
    *,
    task_name: str,
    caption: str,
    model_id: str,
    width: int,
    height: int,
) -> InferenceJobRequest:
    """Build the canonical queue request used by both production and cache audits."""

    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported Ideogram provider task: {task_name}")
    if model_id != IDEOGRAM4_MODEL_ID:
        raise ValueError(f"Ideogram provider requires model {IDEOGRAM4_MODEL_ID!r}")
    validated_caption = validate_ideogram_caption(caption)
    _validate_dimensions(width, height)
    job_id = ideogram_application_job_id(
        task_name=task_name,
        caption=validated_caption,
        width=width,
        height=height,
        model_id=model_id,
    )
    seed = ideogram_seed_for_job(job_id)
    return InferenceJobRequest(
        job_id=job_id,
        task=task_name,
        output=ObjectOutput(
            key=f"jobs/{job_id}/image.png",
            content_type="image/png",
        ),
        parameters={
            "generation_profile": IDEOGRAM4_GENERATION_PROFILE,
            "model_id": model_id,
            "caption": validated_caption,
            "width": width,
            "height": height,
            "seed": seed,
        },
    )


def reference_caption_variants(caption: str, *, task_name: str) -> list[tuple[str, str]]:
    """Return deterministic canonical/fallback captions in execution priority order."""

    canonical = validate_ideogram_caption(caption)
    recovery = _reference_recovery_caption(canonical, task_name=task_name)
    variants = [("canonical", canonical)]
    if recovery != canonical:
        variants.append(("safe_fallback", recovery))
    return variants


class SaladIdeogramImageProvider:
    """Image provider backed by the shared Salad Ideogram 4 Quality queue."""

    def __init__(
        self,
        *,
        executor: InferenceJobExecutor,
        temp_dir: Path,
        task_name: str,
        max_concurrency: int = 1,
    ) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported Ideogram provider task: {task_name}")
        if max_concurrency < 1:
            raise ValueError("Ideogram provider max_concurrency must be at least 1")
        self._executor = executor
        self._temp_dir = temp_dir
        self._task_name = task_name
        self._generation_gate = asyncio.Semaphore(max_concurrency)

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        size: str,
        quality: ImageQuality,
        output_format: ImageFormat,
    ) -> GeneratedImage:
        async with self._generation_gate:
            return await asyncio.to_thread(
                self._generate_image_sync,
                prompt=prompt,
                model=model,
                size=size,
                quality=quality,
                output_format=output_format,
            )

    def _generate_image_sync(
        self,
        *,
        prompt: str,
        model: str,
        size: str,
        quality: ImageQuality,
        output_format: ImageFormat,
    ) -> GeneratedImage:
        if model != IDEOGRAM4_MODEL_ID:
            raise ValueError(f"Ideogram provider requires model {IDEOGRAM4_MODEL_ID!r}")
        if output_format != "png":
            raise ValueError("Ideogram provider currently supports only PNG output")
        if quality not in {"high", "auto"}:
            raise ValueError("Ideogram worker is fixed to Quality mode; use quality='high'")

        width, height = parse_ideogram_size(size)
        variants = reference_caption_variants(prompt, task_name=self._task_name)

        response: InferenceJobResponse | None = None
        selected_variant = "canonical"
        for variant_name, variant_caption in variants:
            request = build_ideogram_job_request(
                task_name=self._task_name,
                caption=variant_caption,
                model_id=model,
                width=width,
                height=height,
            )
            cached = cached_inference_response(self._executor.storage, request)
            if cached is not None:
                response = cached
                selected_variant = variant_name
                break

        if response is None:
            selected_variant, caption = variants[0]
            try:
                response = self._execute_caption(
                    caption=caption,
                    model=model,
                    width=width,
                    height=height,
                    prompt_variant=selected_variant,
                )
            except RemoteInferenceRejectedError as exc:
                if exc.detail != _SAFETY_BLOCK_DETAIL or len(variants) < 2:
                    raise
                selected_variant, recovery_caption = variants[1]
                response = self._execute_caption(
                    caption=recovery_caption,
                    model=model,
                    width=width,
                    height=height,
                    prompt_variant=selected_variant,
                )

        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._temp_dir) as directory:
            destination = Path(directory) / "image.png"
            self._executor.download_output(response, destination)
            content = destination.read_bytes()

        if not content:
            raise RuntimeError("Ideogram 4 worker returned an empty PNG artifact")
        return GeneratedImage(
            content=content,
            media_type="image/png",
            extension="png",
            metadata={
                "prompt_variant": selected_variant,
                "job_id": response.job_id,
                "request_sha256": response.request_sha256,
                "replayed": str(response.replayed).lower(),
            },
        )

    def _execute_caption(
        self,
        *,
        caption: str,
        model: str,
        width: int,
        height: int,
        prompt_variant: str,
    ) -> InferenceJobResponse:
        request = build_ideogram_job_request(
            task_name=self._task_name,
            caption=caption,
            model_id=model,
            width=width,
            height=height,
        )
        purpose = "reference" if self._task_name == IDEOGRAM4_REFERENCE_TASK else "keyframe"
        phase = "4" if purpose == "reference" else "6"
        return self._executor.execute(
            request,
            metadata={
                "phase": phase,
                "provider": "ideogram4",
                "purpose": purpose,
                "prompt_variant": prompt_variant,
            },
        )


def _reference_recovery_caption(caption: str, *, task_name: str) -> str:
    if task_name != IDEOGRAM4_REFERENCE_TASK:
        return caption
    payload = json.loads(caption)
    if not isinstance(payload, dict):
        return caption
    high_level = str(payload.get("high_level_description", "")).strip()
    if not high_level.startswith(_LOCATION_PREFIX):
        return caption
    composition = payload.get("compositional_deconstruction")
    if not isinstance(composition, dict) or composition.get("elements"):
        return caption

    subject = high_level[len(_LOCATION_PREFIX) :].strip()
    subject = subject.split(". ", maxsplit=1)[0].strip()
    subject = " ".join(subject.split()).strip(" ,")
    if not subject:
        return caption
    if not subject.endswith((".", "!", "?")):
        subject += "."

    recovery = {
        "high_level_description": f"{_LOCATION_PREFIX} {subject}",
        "style_description": dict(_RECOVERY_STYLE),
        "compositional_deconstruction": {
            "background": _RECOVERY_BACKGROUND,
            "elements": [],
        },
    }
    rendered = json.dumps(recovery, ensure_ascii=False, separators=(",", ":"))
    return validate_ideogram_caption(rendered)


def parse_ideogram_size(size: str) -> tuple[int, int]:
    parts = size.lower().split("x", maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Ideogram size must use WIDTHxHEIGHT notation")
    try:
        width, height = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("Ideogram size must contain integer dimensions") from exc
    _validate_dimensions(width, height)
    return width, height


def _validate_dimensions(width: int, height: int) -> None:
    if not 256 <= width <= 2048 or not 256 <= height <= 2048:
        raise ValueError("Ideogram dimensions must be between 256 and 2048 pixels")
    if width % 16 or height % 16:
        raise ValueError("Ideogram dimensions must be divisible by 16")
