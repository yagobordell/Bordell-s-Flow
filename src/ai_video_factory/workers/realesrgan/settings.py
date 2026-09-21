from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ai_video_factory.inference.settings import InferenceWorkerSettings


class RealESRGANWorkerSettings(InferenceWorkerSettings):
    """Configuration owned by the self-hosted Real-ESRGAN Salad worker."""

    model_root: Path = Field(
        default=Path("/workspace/models/realesrgan"),
        validation_alias="REALESRGAN_MODEL_ROOT",
    )
    device: str = Field(default="cuda", validation_alias="REALESRGAN_DEVICE")
