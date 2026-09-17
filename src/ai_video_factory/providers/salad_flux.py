from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest, InferenceJobResponse, ObjectOutput
from ai_video_factory.providers.ideogram_caption import validate_ideogram_caption
from ai_video_factory.workers.flux_schnell import (
    FLUX_SCHNELL_GENERATION_PROFILE,
    FLUX_SCHNELL_KEYFRAME_TASK,
    FLUX_SCHNELL_MODEL_ID,
    FLUX_SCHNELL_REFERENCE_TASK,
)

from .images import GeneratedImage, ImageFormat, ImageQuality
from .inference_jobs import InferenceJobExecutor, cached_inference_response

logger = logging.getLogger(__name__)

_SUPPORTED_TASKS = frozenset({FLUX_SCHNELL_REFERENCE_TASK, FLUX_SCHNELL_KEYFRAME_TASK})


def render_flux_prompt(caption: str) -> str:
    """Render the structured visual plan into a deterministic natural-language FLUX prompt."""

    payload = json.loads(validate_ideogram_caption(caption))
    high_level = str(payload.get("high_level_description", "")).strip()
    style = payload.get("style_description") or {}
    composition = payload.get("compositional_deconstruction") or {}

    style_parts = [str(value).strip() for value in style.values() if isinstance(value, str)]
    background = str(composition.get("background", "")).strip()
    element_parts: list[str] = []
    for element in composition.get("elements", []):
        if not isinstance(element, dict):
            continue
        description = str(element.get("desc", "")).strip()
        if description:
            element_parts.append(description)

    parts = [high_level]
    if style_parts:
        parts.append("Style: " + "; ".join(style_parts))
    if background and background != high_level:
        parts.append("Background: " + background)
    if element_parts:
        parts.append("Elements: " + "; ".join(element_parts))
    parts.append("No text, captions, logos, watermarks, borders, or UI overlays.")
    return "\n".join(part for part in parts if part)


def flux_application_job_id(
    *,
    task_name: str,
    prompt: str,
    width: int,
    height: int,
    model_id: str = FLUX_SCHNELL_MODEL_ID,
) -> str:
    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported FLUX task: {task_name}")
    purpose = "reference" if task_name == FLUX_SCHNELL_REFERENCE_TASK else "keyframe"
    payload = {
        "generation_profile": FLUX_SCHNELL_GENERATION_PROFILE,
        "height": height,
        "model_id": model_id,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "purpose": purpose,
        "width": width,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"flux-{purpose}-{hashlib.sha256(canonical).hexdigest()[:32]}"


def flux_seed_for_job(job_id: str) -> int:
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def build_flux_job_request(
    *,
    task_name: str,
    caption: str,
    model_id: str,
    width: int,
    height: int,
) -> InferenceJobRequest:
    if model_id != FLUX_SCHNELL_MODEL_ID:
        raise ValueError(f"FLUX fallback requires model {FLUX_SCHNELL_MODEL_ID!r}")
    prompt = render_flux_prompt(caption)
    job_id = flux_application_job_id(
        task_name=task_name,
        prompt=prompt,
        width=width,
        height=height,
        model_id=model_id,
    )
    return InferenceJobRequest(
        job_id=job_id,
        task=task_name,
        output=ObjectOutput(key=f"jobs/{job_id}/image.png", content_type="image/png"),
        parameters={
            "generation_profile": FLUX_SCHNELL_GENERATION_PROFILE,
            "model_id": model_id,
            "prompt": prompt,
            "width": width,
            "height": height,
            "seed": flux_seed_for_job(job_id),
        },
    )


class SaladFluxSchnellImageProvider:
    """FLUX.1-schnell queue provider used only as the Ideogram safety fallback."""

    def __init__(
        self,
        *,
        executor: InferenceJobExecutor,
        temp_dir: Path,
        task_name: str,
    ) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported FLUX task: {task_name}")
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
        import asyncio

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
        if model != FLUX_SCHNELL_MODEL_ID:
            raise ValueError(f"FLUX fallback requires model {FLUX_SCHNELL_MODEL_ID!r}")
        if quality not in {"high", "auto"}:
            raise ValueError("FLUX fallback supports quality='high' or 'auto'")
        if output_format != "png":
            raise ValueError("FLUX fallback currently supports only PNG output")
        width, height = _parse_size(size)
        request = build_flux_job_request(
            task_name=self._task_name,
            caption=prompt,
            model_id=model,
            width=width,
            height=height,
        )

        response = cached_inference_response(self._executor.storage, request)
        if response is None:
            response = self._execute_request(request)
        else:
            logger.info("FLUX fallback cache hit application_job_id=%s", request.job_id)
        return self._download(response)

    def _execute_request(self, request: InferenceJobRequest) -> InferenceJobResponse:
        purpose = "reference" if self._task_name == FLUX_SCHNELL_REFERENCE_TASK else "keyframe"
        logger.info("FLUX fallback submit application_job_id=%s", request.job_id)
        return self._executor.execute(
            request,
            metadata={
                "fallback_from": "ideogram4",
                "phase": "4" if purpose == "reference" else "6",
                "provider": "flux_schnell",
                "purpose": purpose,
            },
        )

    def _download(self, response: InferenceJobResponse) -> GeneratedImage:
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._temp_dir) as directory:
            destination = Path(directory) / "image.png"
            self._executor.download_output(response, destination)
            content = destination.read_bytes()
        if not content:
            raise RuntimeError("FLUX fallback worker returned an empty PNG artifact")
        return GeneratedImage(
            content=content,
            media_type="image/png",
            extension="png",
            metadata={
                "job_id": response.job_id,
                "model": FLUX_SCHNELL_MODEL_ID,
                "provider": "flux_schnell",
                "replayed": str(response.replayed).lower(),
                "request_sha256": response.request_sha256,
            },
        )


def _parse_size(size: str) -> tuple[int, int]:
    try:
        width_raw, height_raw = size.lower().split("x", maxsplit=1)
        width, height = int(width_raw), int(height_raw)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Image size must use WIDTHxHEIGHT syntax") from exc
    if width < 256 or height < 256 or width > 2048 or height > 2048:
        raise ValueError("FLUX dimensions must be between 256 and 2048 pixels")
    if width % 16 or height % 16:
        raise ValueError("FLUX dimensions must be divisible by 16")
    return width, height
