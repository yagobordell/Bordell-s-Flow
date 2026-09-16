from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
from .ideogram_rejections import (
    is_safety_rejection_detail,
    known_safety_rejection,
    record_safety_rejection,
)
from .images import GeneratedImage, ImageFormat, ImageQuality
from .inference_jobs import (
    InferenceJobExecutor,
    RemoteInferenceRejectedError,
    cached_inference_response,
)

logger = logging.getLogger(__name__)

_SUPPORTED_TASKS = frozenset({IDEOGRAM4_REFERENCE_TASK, IDEOGRAM4_KEYFRAME_TASK})
_CANONICAL_PREFIXES = (
    "Canonical location reference.",
    "Canonical object reference.",
    "Canonical character reference.",
)
_LOCATION_PREFIX = "Canonical location reference."
_LEGACY_RECOVERY_STYLE = {
    "aesthetics": "documentary realism",
    "lighting": "neutral natural daylight with clearly readable form",
    "photo": "realistic reference photography with natural proportions",
    "medium": "documentary photograph",
}
_LEGACY_RECOVERY_BACKGROUND = (
    "Clear reusable environment reference emphasizing stable physical geography, architecture, "
    "materials, layout and recurring landmarks."
)
_LEGACY_CACHE_ONLY_VARIANT = "safe_fallback"


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
    """Return deterministic cache candidates and executable safety variants."""

    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported Ideogram provider task: {task_name}")
    canonical = validate_ideogram_caption(caption)
    payload = json.loads(canonical)
    variants = [("canonical", canonical)]
    if task_name == IDEOGRAM4_REFERENCE_TASK:
        variants.append(
            (
                _LEGACY_CACHE_ONLY_VARIANT,
                _legacy_reference_recovery_caption(canonical),
            )
        )
    variants.extend(
        [
            ("safe_simplified", _safety_recovery_caption(payload, minimal=False)),
            ("safe_minimal_art", _safety_recovery_caption(payload, minimal=True)),
        ]
    )

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, rendered in variants:
        if rendered in seen:
            continue
        seen.add(rendered)
        unique.append((name, rendered))
    return unique


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
        candidates = [
            (
                variant_name,
                build_ideogram_job_request(
                    task_name=self._task_name,
                    caption=variant_caption,
                    model_id=model,
                    width=width,
                    height=height,
                ),
            )
            for variant_name, variant_caption in reference_caption_variants(
                prompt,
                task_name=self._task_name,
            )
        ]

        response: InferenceJobResponse | None = None
        selected_variant = "canonical"
        rejected_job_ids: set[str] = set()

        for variant_name, request in candidates:
            cached = cached_inference_response(self._executor.storage, request)
            if cached is not None:
                logger.info(
                    "Ideogram cache hit prompt_variant=%s application_job_id=%s",
                    variant_name,
                    request.job_id,
                )
                response = cached
                selected_variant = variant_name
                break
            if known_safety_rejection(self._executor.storage, request):
                rejected_job_ids.add(request.job_id)
                logger.warning(
                    "Ideogram cached safety rejection prompt_variant=%s application_job_id=%s; "
                    "queue submission skipped",
                    variant_name,
                    request.job_id,
                )

        last_rejection: RemoteInferenceRejectedError | None = None
        if response is None:
            for variant_name, request in candidates:
                if variant_name == _LEGACY_CACHE_ONLY_VARIANT:
                    logger.info(
                        "Ideogram legacy cache-only variant has no cached output; "
                        "application_job_id=%s queue submission skipped",
                        request.job_id,
                    )
                    continue
                if request.job_id in rejected_job_ids:
                    continue
                try:
                    response = self._execute_request(
                        request=request,
                        prompt_variant=variant_name,
                    )
                    selected_variant = variant_name
                    break
                except RemoteInferenceRejectedError as exc:
                    if not is_safety_rejection_detail(exc.detail):
                        raise
                    record_safety_rejection(
                        self._executor.storage,
                        request,
                        detail=exc.detail,
                        transport_job_id=exc.transport_job_id,
                    )
                    logger.warning(
                        "Ideogram safety rejection prompt_variant=%s application_job_id=%s "
                        "transport_job_id=%s reason=%s",
                        variant_name,
                        request.job_id,
                        exc.transport_job_id,
                        exc.detail,
                    )
                    last_rejection = exc

        if response is None:
            executable = [
                request
                for variant_name, request in candidates
                if variant_name != _LEGACY_CACHE_ONLY_VARIANT
            ]
            last_request = executable[-1]
            raise RemoteInferenceRejectedError(
                last_request.job_id,
                "Ideogram 4 safety filter blocked all provider caption variants",
                transport_job_id=(
                    last_rejection.transport_job_id if last_rejection is not None else None
                ),
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

    def _execute_request(
        self,
        *,
        request: InferenceJobRequest,
        prompt_variant: str,
    ) -> InferenceJobResponse:
        purpose = "reference" if self._task_name == IDEOGRAM4_REFERENCE_TASK else "keyframe"
        phase = "4" if purpose == "reference" else "6"
        logger.info(
            "Ideogram submit prompt_variant=%s application_job_id=%s",
            prompt_variant,
            request.job_id,
        )
        return self._executor.execute(
            request,
            metadata={
                "phase": phase,
                "provider": "ideogram4",
                "purpose": purpose,
                "prompt_variant": prompt_variant,
            },
        )


def _legacy_reference_recovery_caption(caption: str) -> str:
    """Rebuild the pre-v4 fallback exactly so valid historical R2 objects remain hits."""

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
        "style_description": dict(_LEGACY_RECOVERY_STYLE),
        "compositional_deconstruction": {
            "background": _LEGACY_RECOVERY_BACKGROUND,
            "elements": [],
        },
    }
    rendered = json.dumps(recovery, ensure_ascii=False, separators=(",", ":"))
    return validate_ideogram_caption(rendered)


def _safety_recovery_caption(payload: dict[str, Any], *, minimal: bool) -> str:
    composition = payload.get("compositional_deconstruction")
    style = payload.get("style_description")
    if not isinstance(composition, dict) or not isinstance(style, dict):
        raise ValueError("Validated Ideogram caption has an invalid structured payload")

    high_level = _concise_text(str(payload.get("high_level_description", "")))
    background = _concise_text(str(composition.get("background", "")))
    if not background:
        background = high_level

    recovery = {
        "high_level_description": high_level,
        "style_description": _recovery_style(style, minimal=minimal),
        "compositional_deconstruction": {
            "background": background,
            "elements": _recovery_elements(composition.get("elements", []), minimal=minimal),
        },
    }
    rendered = json.dumps(recovery, ensure_ascii=False, separators=(",", ":"))
    return validate_ideogram_caption(rendered)


def _concise_text(value: str) -> str:
    value = " ".join(value.split()).strip()
    for prefix in _CANONICAL_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    first_sentence, separator, _ = value.partition(". ")
    concise = first_sentence.strip()
    if separator and concise and not concise.endswith((".", "!", "?")):
        concise += "."
    return concise or value


def _recovery_style(style: Mapping[str, Any], *, minimal: bool) -> dict[str, Any]:
    palette = style.get("color_palette")
    if minimal:
        recovered: dict[str, Any] = {
            "aesthetics": "clean production visual study",
            "lighting": "clear neutral daylight with readable forms",
            "medium": "production visual reference",
            "art_style": "clean realistic concept art",
        }
    elif "photo" in style:
        recovered = {
            "aesthetics": "neutral production reference",
            "lighting": "clear neutral daylight with readable forms",
            "medium": "reference photograph",
            "photo": "straightforward realistic reference photography",
        }
    else:
        recovered = {
            "aesthetics": "neutral production reference",
            "lighting": "clear neutral daylight with readable forms",
            "medium": "production visual reference",
            "art_style": "clean realistic reference art",
        }
    if isinstance(palette, list) and palette:
        recovered["color_palette"] = palette
    return recovered


def _recovery_elements(elements: Any, *, minimal: bool) -> list[Any]:
    if not isinstance(elements, list) or not minimal:
        return elements if isinstance(elements, list) else []
    recovered: list[Any] = []
    for element in elements:
        if not isinstance(element, dict):
            recovered.append(element)
            continue
        item = dict(element)
        for key in ("desc", "description", "visual_description", "appearance"):
            value = item.get(key)
            if isinstance(value, str):
                item[key] = _concise_text(value)
        recovered.append(item)
    return recovered


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
