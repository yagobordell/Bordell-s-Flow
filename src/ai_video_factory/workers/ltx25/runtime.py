from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .a2v import DirectLTX25AudioToVideoBackend, LTXAudioToVideoTaskRunner
from .model import DirectLTX25Backend, LTXPipelineModeController, LTXVideoTaskRunner
from .settings import LTX25WorkerSettings


def build_ltx25_worker(settings: LTX25WorkerSettings):
    mode_controller = LTXPipelineModeController()
    video_backend = DirectLTX25Backend(
        model_root=settings.model_root,
        device=settings.device,
        mode_controller=mode_controller,
    )
    audio_to_video_backend = DirectLTX25AudioToVideoBackend(
        model_root=settings.model_root,
        device=settings.device,
        mode_controller=mode_controller,
    )
    runners = TaskRunnerRegistry(
        [
            LTXVideoTaskRunner(backend=video_backend),
            LTXAudioToVideoTaskRunner(backend=audio_to_video_backend),
        ]
    )
    return build_worker(settings, runners=runners)


runtime_settings = LTX25WorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_ltx25_worker(runtime_settings),
    prepare_in_background=True,
)
