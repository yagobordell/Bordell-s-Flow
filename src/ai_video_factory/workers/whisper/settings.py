from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from ai_video_factory.inference.settings import InferenceWorkerSettings


class WhisperWorkerSettings(InferenceWorkerSettings):
    """Configuration owned by the dedicated Whisper worker image."""

    model_root: Path = Field(
        default=Path("/workspace/models/whisper-large-v3-turbo"),
        validation_alias="WHISPER_MODEL_ROOT",
    )
    model_repository: str = Field(
        default="openai/whisper-large-v3-turbo",
        validation_alias="WHISPER_MODEL_REPOSITORY",
    )
    model_revision: str = Field(default="main", validation_alias="WHISPER_MODEL_REVISION")
    device: str = Field(default="cuda:0", validation_alias="WHISPER_DEVICE")
    dtype: str = Field(default="float16", validation_alias="WHISPER_DTYPE")

    @model_validator(mode="after")
    def validate_whisper(self) -> Self:
        for name, value in (
            ("WHISPER_MODEL_REPOSITORY", self.model_repository),
            ("WHISPER_MODEL_REVISION", self.model_revision),
            ("WHISPER_DEVICE", self.device),
            ("WHISPER_DTYPE", self.dtype),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")
        return self
