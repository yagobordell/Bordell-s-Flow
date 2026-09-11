from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from ai_video_factory.inference.settings import InferenceWorkerSettings


class LTX25WorkerSettings(InferenceWorkerSettings):
    """Configuration owned by the dedicated LTX-2.5 worker image."""

    model_root: Path = Field(
        default=Path("/workspace/models/ltx-2.5"),
        validation_alias="LTX_MODEL_ROOT",
    )
    model_repository: str = Field(
        default="Lightricks/LTX-2.5",
        validation_alias="LTX_MODEL_REPOSITORY",
    )
    device: str = Field(default="cuda", validation_alias="LTX_DEVICE")

    @model_validator(mode="after")
    def validate_ltx25(self) -> Self:
        if not self.model_repository.strip():
            raise ValueError("LTX_MODEL_REPOSITORY must be non-empty")
        if not self.device.strip():
            raise ValueError("LTX_DEVICE must be non-empty")
        return self
