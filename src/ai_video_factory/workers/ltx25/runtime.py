from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .a2v import DirectLTX25A2VBackend, LTXA2VTaskRunner
from .model import DirectLTX25Backend, LTXVideoTaskRunner
from .settings import LTX25WorkerSettings


def build_ltx25_worker(settings: LTX25WorkerSettings):
    backend = DirectLTX25Backend(
        model_root=settings.model_root,
        device=settings.device,
    )
    a2v_backend = DirectLTX25A2VBackend(
        model_root=settings.model_root,
        device=settings.device,
    )
    runners = TaskRunnerRegistry(
        [
            LTXVideoTaskRunner(backend=backend),
            LTXA2VTaskRunner(backend=a2v_backend),
        ]
    )
    return build_worker(settings, runners=runners)


runtime_settings = LTX25WorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_ltx25_worker(runtime_settings),
    prepare_in_background=True,
)
