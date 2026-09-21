from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from ai_video_factory.inference.settings import InferenceWorkerSettings


class BreezeTTS2WorkerSettings(InferenceWorkerSettings):
    """Configuration owned by the dedicated Breeze TTS 2 worker image."""

    model_root: Path = Field(
        default=Path("/workspace/models/breeze-tts-2"),
        validation_alias="BREEZE_MODEL_ROOT",
    )
    model_repository: str = Field(
        default="BreezeBlue/Breeze-TTS-2",
        validation_alias="BREEZE_MODEL_REPOSITORY",
    )
    model_revision: str = Field(default="main", validation_alias="BREEZE_MODEL_REVISION")
    runtime_root: Path = Field(
        default=Path("/opt/breeze-infer"),
        validation_alias="BREEZE_RUNTIME_ROOT",
    )
    device: str = Field(default="cuda", validation_alias="BREEZE_DEVICE")
    max_chunk_chars: int = Field(
        default=4000,
        ge=200,
        le=10_000,
        validation_alias="BREEZE_MAX_CHUNK_CHARS",
    )
    inter_chunk_pause_ms: int = Field(
        default=120,
        ge=0,
        le=2000,
        validation_alias="BREEZE_INTER_CHUNK_PAUSE_MS",
    )

    @model_validator(mode="after")
    def validate_breeze(self) -> Self:
        for name, value in (
            ("BREEZE_MODEL_REPOSITORY", self.model_repository),
            ("BREEZE_MODEL_REVISION", self.model_revision),
            ("BREEZE_DEVICE", self.device),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")
        return self
