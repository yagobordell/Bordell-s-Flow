from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .model import BreezeSpeechTaskRunner, BreezeTTS2Backend
from .settings import BreezeTTS2WorkerSettings


def build_breeze_worker(settings: BreezeTTS2WorkerSettings):
    backend = BreezeTTS2Backend(
        model_root=settings.model_root,
        model_repository=settings.model_repository,
        model_revision=settings.model_revision,
        runtime_root=settings.runtime_root,
        device=settings.device,
        max_chunk_chars=settings.max_chunk_chars,
        inter_chunk_pause_ms=settings.inter_chunk_pause_ms,
    )
    runners = TaskRunnerRegistry([BreezeSpeechTaskRunner(backend=backend)])
    return build_worker(settings, runners=runners)


runtime_settings = BreezeTTS2WorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_breeze_worker(runtime_settings),
    prepare_in_background=True,
)
