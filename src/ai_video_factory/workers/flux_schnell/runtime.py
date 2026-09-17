from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .model import (
    FLUX_SCHNELL_KEYFRAME_TASK,
    FLUX_SCHNELL_REFERENCE_TASK,
    FluxSchnellBackend,
    FluxSchnellImageTaskRunner,
)
from .settings import FluxSchnellWorkerSettings


def build_flux_schnell_worker(settings: FluxSchnellWorkerSettings):
    backend = FluxSchnellBackend(
        model_root=settings.model_root,
        model_snapshot=settings.model_snapshot,
        model_repository=settings.model_repository,
        model_revision=settings.model_revision,
        device=settings.device,
        inference_steps=settings.inference_steps,
        max_sequence_length=settings.max_sequence_length,
    )
    runners = TaskRunnerRegistry(
        [
            FluxSchnellImageTaskRunner(
                backend=backend,
                task_name=FLUX_SCHNELL_REFERENCE_TASK,
            ),
            FluxSchnellImageTaskRunner(
                backend=backend,
                task_name=FLUX_SCHNELL_KEYFRAME_TASK,
            ),
        ]
    )
    return build_worker(settings, runners=runners)


runtime_settings = FluxSchnellWorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_flux_schnell_worker(runtime_settings),
    prepare_in_background=True,
)
