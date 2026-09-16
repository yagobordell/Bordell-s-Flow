from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .model import (
    IDEOGRAM4_KEYFRAME_TASK,
    IDEOGRAM4_REFERENCE_TASK,
    Ideogram4Backend,
    IdeogramImageTaskRunner,
)
from .settings import Ideogram4WorkerSettings


def build_ideogram4_worker(settings: Ideogram4WorkerSettings):
    backend = Ideogram4Backend(
        model_root=settings.model_root,
        model_repository=settings.model_repository,
        model_revision=settings.model_revision,
        bootstrap_status_path=settings.bootstrap_status_path,
        device=settings.device,
        sampler_preset=settings.sampler_preset,
    )
    runners = TaskRunnerRegistry(
        [
            IdeogramImageTaskRunner(
                backend=backend,
                task_name=IDEOGRAM4_REFERENCE_TASK,
            ),
            IdeogramImageTaskRunner(
                backend=backend,
                task_name=IDEOGRAM4_KEYFRAME_TASK,
            ),
        ]
    )
    return build_worker(settings, runners=runners)


runtime_settings = Ideogram4WorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_ideogram4_worker(runtime_settings),
    prepare_in_background=True,
)
