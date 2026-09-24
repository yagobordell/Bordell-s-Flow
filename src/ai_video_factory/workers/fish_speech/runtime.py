from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .model import FishSpeechBackend, FishSpeechTaskRunner
from .settings import FishSpeechWorkerSettings


def build_fish_speech_worker(settings: FishSpeechWorkerSettings):
    backend = FishSpeechBackend(
        model_root=settings.model_root,
        model_repository=settings.model_repository,
        model_revision=settings.model_revision,
        runtime_commit=settings.runtime_commit,
        device=settings.device,
        max_chunk_bytes=settings.max_chunk_bytes,
        inter_chunk_pause_ms=settings.inter_chunk_pause_ms,
    )
    runners = TaskRunnerRegistry([FishSpeechTaskRunner(backend=backend)])
    return build_worker(settings, runners=runners)


runtime_settings = FishSpeechWorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_fish_speech_worker(runtime_settings),
    prepare_in_background=True,
    poll_jobs_from_repository=runtime_settings.worker_poll_jobs,
    job_poll_seconds=runtime_settings.worker_job_poll_seconds,
)
