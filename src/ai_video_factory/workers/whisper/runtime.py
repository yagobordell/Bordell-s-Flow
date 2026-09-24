from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .model import TransformersWhisperBackend, WhisperTaskRunner
from .settings import WhisperWorkerSettings


def build_whisper_worker(settings: WhisperWorkerSettings):
    backend = TransformersWhisperBackend(
        model_root=settings.model_root,
        device=settings.device,
        dtype=settings.dtype,
    )
    runners = TaskRunnerRegistry([WhisperTaskRunner(backend=backend)])
    return build_worker(settings, runners=runners)


runtime_settings = WhisperWorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_whisper_worker(runtime_settings),
    prepare_in_background=True,
    poll_jobs_from_repository=runtime_settings.worker_poll_jobs,
    job_poll_seconds=runtime_settings.worker_job_poll_seconds,
)
