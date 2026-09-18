from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ai_video_factory.inference.settings import InferenceWorkerSettings


class Flux2KleinWorkerSettings(InferenceWorkerSettings):
    model_root: Path = Field(
        default=Path("/workspace/models/flux2-klein-4b"),
        validation_alias="FLUX2_KLEIN_MODEL_ROOT",
    )
    device: str = Field(default="cuda", validation_alias="FLUX2_KLEIN_DEVICE")
