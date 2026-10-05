from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

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
    task_mode: Literal["both", "i2v", "a2v", "a2v_experimental"] = Field(
        default="both", validation_alias="LTX_WORKER_TASK_MODE"
    )

    experimental_a2v_batch2_enabled: bool = Field(
        default=False, validation_alias="LTX_EXPERIMENTAL_A2V_BATCH2_ENABLED",
    )

    @model_validator(mode="after")
    def validate_ltx25(self) -> Self:
        if self.experimental_a2v_batch2_enabled and self.task_mode != "a2v_experimental":
            raise ValueError(
                "A2V batch2 may only register in the isolated a2v_experimental group"
            )
        if not self.model_repository.strip():
            raise ValueError("LTX_MODEL_REPOSITORY must be non-empty")
        if not self.device.strip():
            raise ValueError("LTX_DEVICE must be non-empty")
        if self.worker_mode == "production" and self.worker_poll_jobs and self.task_mode == "both":
            raise ValueError(
                "Production LTX polling requires explicit i2v, a2v, "
                "or a2v_experimental mode"
            )
        return self
