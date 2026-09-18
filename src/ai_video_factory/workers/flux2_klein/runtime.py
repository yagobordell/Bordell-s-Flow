from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .model import (
    FLUX2_KLEIN_KEYFRAME_TASK,
    FLUX2_KLEIN_REFERENCE_TASK,
    Flux2KleinBackend,
    Flux2KleinImageTaskRunner,
)
from .settings import Flux2KleinWorkerSettings


def build_flux2_klein_worker(settings: Flux2KleinWorkerSettings):
    backend = Flux2KleinBackend(
        model_root=settings.model_root,
        model_repository=settings.model_repository,
        model_revision=settings.model_revision,
        bootstrap_status_path=settings.bootstrap_status_path,
        device=settings.device,
    )
    runners = TaskRunnerRegistry(
        [
            Flux2KleinImageTaskRunner(
                backend=backend,
                task_name=FLUX2_KLEIN_REFERENCE_TASK,
            ),
            Flux2KleinImageTaskRunner(
                backend=backend,
                task_name=FLUX2_KLEIN_KEYFRAME_TASK,
            ),
        ]
    )
    return build_worker(settings, runners=runners)


runtime_settings = Flux2KleinWorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_flux2_klein_worker(runtime_settings),
    prepare_in_background=True,
)
