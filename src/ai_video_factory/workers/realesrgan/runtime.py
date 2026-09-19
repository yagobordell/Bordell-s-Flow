from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .model import DirectRealESRGANBackend, RealESRGANVideoTaskRunner
from .settings import RealESRGANWorkerSettings


def build_realesrgan_worker(settings: RealESRGANWorkerSettings):
    backend = DirectRealESRGANBackend(
        model_root=settings.model_root,
        device=settings.device,
    )
    runners = TaskRunnerRegistry([RealESRGANVideoTaskRunner(backend=backend)])
    return build_worker(settings, runners=runners)


runtime_settings = RealESRGANWorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_realesrgan_worker(runtime_settings),
    prepare_in_background=True,
)
