from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.workers.qwen_image_21 import (
    QWEN_IMAGE_21_DEFAULT_STEPS,
    QWEN_IMAGE_21_GENERATION_PROFILE,
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_MODEL_ID,
    QWEN_IMAGE_21_MODEL_REVISION,
    QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    QWEN_IMAGE_21_PRODUCTION_SIZE,
    QWEN_IMAGE_21_PRODUCTION_WIDTH,
    QWEN_IMAGE_21_REFERENCE_TASK,
    QWEN_IMAGE_21_TRUE_CFG_SCALE,
    QWEN_IMAGE_21_USE_KV_CACHE,
    qwen_image_21_application_job_id,
    qwen_image_21_seed_for_job,
)

from .images import GeneratedImage, ImageFormat, ImageQuality
from .inference_jobs import InferenceJobExecutor, cached_inference_response

_SUPPORTED_TASKS = frozenset({QWEN_IMAGE_21_REFERENCE_TASK, QWEN_IMAGE_21_KEYFRAME_TASK})


def build_qwen_image_job_request(
    *,
    task_name: str,
    prompt: str,
    model_id: str,
    width: int,
    height: int,
) -> InferenceJobRequest:
    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported Qwen image provider task: {task_name}")
    if model_id != QWEN_IMAGE_21_MODEL_ID:
        raise ValueError(f"Qwen provider requires model {QWEN_IMAGE_21_MODEL_ID!r}")
    rendered = prompt.strip()
    job_id = qwen_image_21_application_job_id(
        task_name=task_name,
        prompt=rendered,
        width=width,
        height=height,
        model_id=model_id,
    )
    return InferenceJobRequest(
        job_id=job_id,
        task=task_name,
        output=ObjectOutput(key=f"jobs/{job_id}/image.png", content_type="image/png"),
        parameters={
            "generation_profile": QWEN_IMAGE_21_GENERATION_PROFILE,
            "model_id": model_id,
            "model_revision": QWEN_IMAGE_21_MODEL_REVISION,
            "prompt": rendered,
            "width": width,
            "height": height,
            "seed": qwen_image_21_seed_for_job(job_id),
            "num_inference_steps": QWEN_IMAGE_21_DEFAULT_STEPS,
            "true_cfg_scale": QWEN_IMAGE_21_TRUE_CFG_SCALE,
            "use_kv_cache": QWEN_IMAGE_21_USE_KV_CACHE,
        },
    )


class SaladQwenImage21Provider:
    def __init__(self, *, executor: InferenceJobExecutor, temp_dir: Path, task_name: str) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported Qwen provider task: {task_name}")
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
        if model != QWEN_IMAGE_21_MODEL_ID:
            raise ValueError(f"Qwen provider requires model {QWEN_IMAGE_21_MODEL_ID!r}")
        if quality not in {"high", "auto"}:
            raise ValueError("Qwen provider supports quality='high' or 'auto'")
        if output_format != "png":
            raise ValueError("Qwen provider supports only PNG output")
        width, height = _parse_size(size)
        request = build_qwen_image_job_request(
            task_name=self._task_name,
            prompt=prompt,
            model_id=model,
            width=width,
            height=height,
        )
        response = cached_inference_response(self._executor.storage, request)
        if response is None:
            purpose = "reference" if self._task_name == QWEN_IMAGE_21_REFERENCE_TASK else "keyframe"
            response = self._executor.execute(
                request,
                metadata={
                    "phase": "4" if purpose == "reference" else "6",
                    "provider": "qwen_image_21",
                    "purpose": purpose,
                    "model": QWEN_IMAGE_21_MODEL_ID,
                    "model_revision": QWEN_IMAGE_21_MODEL_REVISION,
                },
            )
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._temp_dir) as directory:
            destination = Path(directory) / "image.png"
            self._executor.download_output(response, destination)
            content = destination.read_bytes()
        if not content:
            raise RuntimeError("Qwen provider returned an empty PNG artifact")
        return GeneratedImage(
            content=content,
            media_type="image/png",
            extension="png",
            metadata={
                "provider": "qwen_image_21",
                "model": QWEN_IMAGE_21_MODEL_ID,
                "model_revision": QWEN_IMAGE_21_MODEL_REVISION,
                "job_id": response.job_id,
                "request_sha256": response.request_sha256,
                "replayed": str(response.replayed).lower(),
                "prompt_sha256": hashlib.sha256(prompt.strip().encode()).hexdigest(),
            },
        )


def _parse_size(size: str) -> tuple[int, int]:
    try:
        width_text, height_text = size.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Qwen image size: {size!r}") from exc
    if (width, height) != (
        QWEN_IMAGE_21_PRODUCTION_WIDTH,
        QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    ):
        raise ValueError(
            f"Qwen production size must be exactly {QWEN_IMAGE_21_PRODUCTION_SIZE}"
        )
    return width, height
