from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from ai_video_factory.inference.settings import InferenceWorkerSettings

from .model import (
    FISH_SPEECH_MODEL_ID,
    FISH_SPEECH_MODEL_REVISION,
    FISH_SPEECH_RUNTIME_COMMIT,
)


class FishSpeechWorkerSettings(InferenceWorkerSettings):
    """Configuration owned by the dedicated self-hosted Fish Speech worker."""

    model_root: Path = Field(
        default=Path("/workspace/models/fish-s2-pro"),
        validation_alias="FISH_SPEECH_MODEL_ROOT",
    )
    model_repository: str = Field(
        default=FISH_SPEECH_MODEL_ID,
        validation_alias="FISH_SPEECH_MODEL_REPOSITORY",
    )
    model_revision: str = Field(
        default=FISH_SPEECH_MODEL_REVISION,
        validation_alias="FISH_SPEECH_MODEL_REVISION",
    )
    runtime_commit: str = Field(
        default=FISH_SPEECH_RUNTIME_COMMIT,
        validation_alias="FISH_SPEECH_RUNTIME_COMMIT",
    )
    device: str = Field(default="cuda", validation_alias="FISH_SPEECH_DEVICE")
    max_chunk_bytes: int = Field(
        default=800,
        ge=200,
        le=4000,
        validation_alias="FISH_SPEECH_MAX_CHUNK_BYTES",
    )
    inter_chunk_pause_ms: int = Field(
        default=80,
        ge=0,
        le=2000,
        validation_alias="FISH_SPEECH_INTER_CHUNK_PAUSE_MS",
    )

    @model_validator(mode="after")
    def validate_fish(self) -> Self:
        if self.model_repository != FISH_SPEECH_MODEL_ID:
            raise ValueError("FISH_SPEECH_MODEL_REPOSITORY must match the pinned model")
        if self.model_revision != FISH_SPEECH_MODEL_REVISION:
            raise ValueError("FISH_SPEECH_MODEL_REVISION must match the pinned revision")
        if self.runtime_commit != FISH_SPEECH_RUNTIME_COMMIT:
            raise ValueError("FISH_SPEECH_RUNTIME_COMMIT must match the pinned runtime")
        if not self.device.strip():
            raise ValueError("FISH_SPEECH_DEVICE must be non-empty")
        return self
