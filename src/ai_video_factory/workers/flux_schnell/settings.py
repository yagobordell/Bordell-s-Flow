from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from ai_video_factory.inference.settings import InferenceWorkerSettings


class FluxSchnellWorkerSettings(InferenceWorkerSettings):
    """Configuration owned by the dedicated FLUX.1-schnell fallback worker."""

    model_root: Path = Field(
        default=Path("/workspace/models/flux-schnell"),
        validation_alias="FLUX_MODEL_ROOT",
    )
    model_snapshot: Path = Field(
        default=Path("/workspace/models/flux-schnell/snapshot"),
        validation_alias="FLUX_MODEL_LOCAL_SNAPSHOT",
    )
    model_repository: str = Field(
        default="black-forest-labs/FLUX.1-schnell",
        validation_alias="FLUX_MODEL_REPOSITORY",
    )
    model_revision: str = Field(default="main", validation_alias="FLUX_MODEL_REVISION")
    device: str = Field(default="cuda", validation_alias="FLUX_DEVICE")
    dtype: str = Field(default="bfloat16", validation_alias="FLUX_DTYPE")
    quantization: str = Field(default="bnb4-nf4", validation_alias="FLUX_QUANTIZATION")
    inference_steps: int = Field(default=4, ge=1, le=4, validation_alias="FLUX_INFERENCE_STEPS")
    max_sequence_length: int = Field(
        default=256,
        ge=64,
        le=512,
        validation_alias="FLUX_MAX_SEQUENCE_LENGTH",
    )

    @model_validator(mode="after")
    def validate_flux(self) -> Self:
        for name, value in (
            ("FLUX_MODEL_REPOSITORY", self.model_repository),
            ("FLUX_MODEL_REVISION", self.model_revision),
            ("FLUX_DEVICE", self.device),
            ("FLUX_DTYPE", self.dtype),
            ("FLUX_QUANTIZATION", self.quantization),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.model_repository != "black-forest-labs/FLUX.1-schnell":
            raise ValueError("FLUX fallback worker is pinned to black-forest-labs/FLUX.1-schnell")
        if self.dtype != "bfloat16":
            raise ValueError("FLUX fallback worker requires bfloat16 compute")
        if self.quantization != "bnb4-nf4":
            raise ValueError("FLUX fallback worker requires bnb4-nf4 quantization")
        return self
