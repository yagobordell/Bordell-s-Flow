from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.workers.flux2_klein import (
    FLUX2_KLEIN_GENERATION_PROFILE,
    FLUX2_KLEIN_KEYFRAME_TASK,
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_REFERENCE_TASK,
    flux2_klein_application_job_id,
    flux2_klein_seed_for_job,
)

from .images import GeneratedImage, ImageFormat, ImageQuality
from .inference_jobs import InferenceJobExecutor, cached_inference_response

_SUPPORTED_TASKS = frozenset({FLUX2_KLEIN_REFERENCE_TASK, FLUX2_KLEIN_KEYFRAME_TASK})


def render_flux_prompt(prompt: str) -> str:
    """Render the canonical structured image prompt into deterministic FLUX prose."""

    try:
        payload = json.loads(prompt)
    except json.JSONDecodeError:
        return prompt.strip()
    if not isinstance(payload, dict):
        return prompt.strip()

    parts: list[str] = []
    high_level = payload.get("high_level_description")
    if isinstance(high_level, str) and high_level.strip():
        parts.append(high_level.strip())
    style = payload.get("style_description")
    if isinstance(style, dict):
        for key in ("aesthetics", "lighting", "photo", "medium"):
            value = style.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    composition = payload.get("compositional_deconstruction")
    if isinstance(composition, dict):
        background = composition.get("background")
        if isinstance(background, str) and background.strip():
            parts.append(background.strip())
        elements = composition.get("elements")
        if isinstance(elements, list):
            parts.extend(str(item).strip() for item in elements if str(item).strip())
    return ". ".join(dict.fromkeys(parts)) or prompt.strip()


def build_flux_job_request(
    *,
    task_name: str,
    prompt: str,
    model_id: str,
    width: int,
    height: int,
) -> InferenceJobRequest:
    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported FLUX provider task: {task_name}")
    if model_id != FLUX2_KLEIN_MODEL_ID:
        raise ValueError(f"FLUX provider requires model {FLUX2_KLEIN_MODEL_ID!r}")
    rendered = render_flux_prompt(prompt)
    job_id = flux2_klein_application_job_id(
        task_name=task_name,
        prompt=rendered,
        width=width,
        height=height,
        model_id=model_id,
    )
    return InferenceJobRequest(
        job_id=job_id,
        task=task_name,
        output=ObjectOutput(
            key=f"jobs/{job_id}/image.png",
            content_type="image/png",
        ),
        parameters={
            "generation_profile": FLUX2_KLEIN_GENERATION_PROFILE,
            "model_id": model_id,
            "prompt": rendered,
            "width": width,
            "height": height,
            "seed": flux2_klein_seed_for_job(job_id),
            "num_inference_steps": 4,
            "guidance_scale": 1.0,
        },
    )


class SaladFlux2KleinImageProvider:
    def __init__(
        self,
        *,
        executor: InferenceJobExecutor,
        temp_dir: Path,
        task_name: str,
    ) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported FLUX provider task: {task_name}")
        self._executor = executor
        self._temp_dir = temp_dir
        self._task_name = task_name

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        size: str,
        quality: ImageQuality,
        output_format: ImageFormat,
    ) -> GeneratedImage:
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
        if model != FLUX2_KLEIN_MODEL_ID:
            raise ValueError(f"FLUX provider requires model {FLUX2_KLEIN_MODEL_ID!r}")
        if quality not in {"high", "auto"}:
            raise ValueError("FLUX fallback supports quality='high' or 'auto'")
        if output_format != "png":
            raise ValueError("FLUX fallback supports only PNG output")
        width, height = _parse_size(size)
        request = build_flux_job_request(
            task_name=self._task_name,
            prompt=prompt,
            model_id=model,
            width=width,
            height=height,
        )
        response = cached_inference_response(self._executor.storage, request)
        if response is None:
            purpose = "reference" if self._task_name == FLUX2_KLEIN_REFERENCE_TASK else "keyframe"
            response = self._executor.execute(
                request,
                metadata={
                    "phase": "4" if purpose == "reference" else "6",
                    "provider": "flux2_klein",
                    "purpose": purpose,
                    "fallback_from": "ideogram4",
                },
            )
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._temp_dir) as directory:
            destination = Path(directory) / "image.png"
            self._executor.download_output(response, destination)
            content = destination.read_bytes()
        if not content:
            raise RuntimeError("FLUX fallback returned an empty PNG artifact")
        return GeneratedImage(
            content=content,
            media_type="image/png",
            extension="png",
            metadata={
                "provider": "flux2_klein",
                "model": FLUX2_KLEIN_MODEL_ID,
                "fallback_from": "ideogram4",
                "fallback_reason": "safety_rejection",
                "job_id": response.job_id,
                "request_sha256": response.request_sha256,
                "replayed": str(response.replayed).lower(),
                "prompt_sha256": hashlib.sha256(render_flux_prompt(prompt).encode()).hexdigest(),
            },
        )


def _parse_size(size: str) -> tuple[int, int]:
    try:
        width_text, height_text = size.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid FLUX image size: {size!r}") from exc
    if width % 16 or height % 16 or width < 256 or height < 256:
        raise ValueError("FLUX dimensions must be >=256 and divisible by 16")
    return width, height
