from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ai_video_factory.inference.settings import InferenceWorkerSettings


class FluxSchnellWorkerSettings(InferenceWorkerSettings):
    model_root: Path = Field(
        default=Path("/workspace/models/flux-schnell"),
        validation_alias="FLUX_MODEL_ROOT",
    )
    device: str = Field(default="cuda", validation_alias="FLUX_DEVICE")
