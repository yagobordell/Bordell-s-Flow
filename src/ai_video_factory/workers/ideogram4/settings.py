from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from ai_video_factory.inference.settings import InferenceWorkerSettings


class Ideogram4WorkerSettings(InferenceWorkerSettings):
    """Configuration owned by the dedicated Ideogram 4 worker image."""

    model_root: Path = Field(
        default=Path("/workspace/models/ideogram4"),
        validation_alias="IDEOGRAM_MODEL_ROOT",
    )
    model_repository: str = Field(
        default="ideogram-ai/ideogram-4-nf4",
        validation_alias="IDEOGRAM_MODEL_REPOSITORY",
    )
    model_revision: str = Field(default="main", validation_alias="IDEOGRAM_MODEL_REVISION")
    device: str = Field(default="cuda", validation_alias="IDEOGRAM_DEVICE")
    dtype: str = Field(default="bfloat16", validation_alias="IDEOGRAM_DTYPE")
    sampler_preset: str = Field(
        default="V4_QUALITY_48",
        validation_alias="IDEOGRAM_SAMPLER_PRESET",
    )
    bootstrap_status_path: Path = Field(
        default=Path("/tmp/ai-video-factory/ideogram-bootstrap.json"),
        validation_alias="IDEOGRAM_BOOTSTRAP_STATUS_PATH",
    )

    @model_validator(mode="after")
    def validate_ideogram(self) -> Self:
        for name, value in (
            ("IDEOGRAM_MODEL_REPOSITORY", self.model_repository),
            ("IDEOGRAM_MODEL_REVISION", self.model_revision),
            ("IDEOGRAM_DEVICE", self.device),
            ("IDEOGRAM_DTYPE", self.dtype),
            ("IDEOGRAM_SAMPLER_PRESET", self.sampler_preset),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.sampler_preset != "V4_QUALITY_48":
            raise ValueError("Ideogram worker must use the V4_QUALITY_48 quality preset")
        if self.dtype != "bfloat16":
            raise ValueError("Ideogram NF4 worker currently requires bfloat16 compute")
        return self
