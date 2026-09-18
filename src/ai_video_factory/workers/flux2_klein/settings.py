from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ai_video_factory.inference.settings import InferenceWorkerSettings


class Flux2KleinWorkerSettings(InferenceWorkerSettings):
    model_root: Path = Field(
        default=Path("/workspace/models/flux2-klein-4b"),
        validation_alias="FLUX2_KLEIN_MODEL_ROOT",
    )
    model_repository: str = Field(
        default="black-forest-labs/FLUX.2-klein-4B",
        validation_alias="FLUX2_KLEIN_MODEL_REPOSITORY",
    )
    model_revision: str = Field(
        default="e7b7dc27f91deacad38e78976d1f2b499d76a294",
        validation_alias="FLUX2_KLEIN_MODEL_REVISION",
    )
    device: str = Field(default="cuda", validation_alias="FLUX2_KLEIN_DEVICE")
    bootstrap_status_path: Path = Field(
        default=Path("/tmp/ai-video-factory/flux2-klein-bootstrap.json"),
        validation_alias="FLUX2_KLEIN_BOOTSTRAP_STATUS_PATH",
    )
