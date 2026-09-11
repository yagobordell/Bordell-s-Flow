from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
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
from .inference_jobs import InferenceJobExecutor

_SUPPORTED_TASKS = frozenset({IDEOGRAM4_REFERENCE_TASK, IDEOGRAM4_KEYFRAME_TASK})


class SaladIdeogramImageProvider:
    """Image provider backed by the shared Salad Ideogram 4 Quality queue."""

    def __init__(
        self,
        *,
        executor: InferenceJobExecutor,
        temp_dir: Path,
        task_name: str,
    ) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported Ideogram provider task: {task_name}")
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
        if model != IDEOGRAM4_MODEL_ID:
            raise ValueError(f"Ideogram provider requires model {IDEOGRAM4_MODEL_ID!r}")
        if output_format != "png":
            raise ValueError("Ideogram provider currently supports only PNG output")
        if quality not in {"high", "auto"}:
            raise ValueError("Ideogram worker is fixed to Quality mode; use quality='high'")

        caption = validate_ideogram_caption(prompt)
        width, height = _parse_size(size)
        job_id = ideogram_application_job_id(
            task_name=self._task_name,
            caption=caption,
            width=width,
            height=height,
            model_id=model,
        )
        seed = ideogram_seed_for_job(job_id)
        request = InferenceJobRequest(
            job_id=job_id,
            task=self._task_name,
            output=ObjectOutput(
                key=f"jobs/{job_id}/image.png",
                content_type="image/png",
            ),
            parameters={
                "generation_profile": IDEOGRAM4_GENERATION_PROFILE,
                "model_id": model,
                "caption": caption,
                "width": width,
                "height": height,
                "seed": seed,
            },
        )
        purpose = (
            "reference"
            if self._task_name == IDEOGRAM4_REFERENCE_TASK
            else "keyframe"
        )
        phase = "4" if purpose == "reference" else "6"
        response = self._executor.execute(
            request,
            metadata={
                "phase": phase,
                "provider": "ideogram4",
                "purpose": purpose,
            },
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
        )


def _parse_size(size: str) -> tuple[int, int]:
    parts = size.lower().split("x", maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Ideogram size must use WIDTHxHEIGHT notation")
    try:
        width, height = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("Ideogram size must contain integer dimensions") from exc
    if not 256 <= width <= 2048 or not 256 <= height <= 2048:
        raise ValueError("Ideogram dimensions must be between 256 and 2048 pixels")
    if width % 16 or height % 16:
        raise ValueError("Ideogram dimensions must be divisible by 16")
    return width, height
