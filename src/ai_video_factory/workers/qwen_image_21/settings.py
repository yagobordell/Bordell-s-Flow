from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ai_video_factory.inference.settings import InferenceWorkerSettings


class QwenImage21WorkerSettings(InferenceWorkerSettings):
    model_root: Path = Field(
        default=Path("/workspace/models/qwen-image-2.1"),
        validation_alias="QWEN_IMAGE_21_MODEL_ROOT",
    )
    device: str = Field(default="cuda", validation_alias="QWEN_IMAGE_21_DEVICE")
