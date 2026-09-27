from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .a2v import DirectLTX25AudioToVideoBackend, LTXAudioToVideoTaskRunner
from .model import DirectLTX25Backend, LTXPipelineModeController, LTXVideoTaskRunner
from .settings import LTX25WorkerSettings


def build_ltx25_worker(settings: LTX25WorkerSettings):
    # Register only the tasks owned by this Salad service. The Postgres poller
    # selects by registered task name, and /jobs rejects any other task.
    mode_controller = LTXPipelineModeController()
    runners = []
    if settings.task_mode in ("i2v", "both"):
        video_backend = DirectLTX25Backend(
            model_root=settings.model_root,
            device=settings.device,
            mode_controller=mode_controller,
        )
        runners.append(LTXVideoTaskRunner(backend=video_backend))
    if settings.task_mode in ("a2v", "both"):
        audio_to_video_backend = DirectLTX25AudioToVideoBackend(
            model_root=settings.model_root,
            device=settings.device,
            mode_controller=mode_controller,
        )
        runners.append(LTXAudioToVideoTaskRunner(backend=audio_to_video_backend))
    return build_worker(settings, runners=TaskRunnerRegistry(runners))


runtime_settings = LTX25WorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_ltx25_worker(runtime_settings),
    prepare_in_background=True,
    poll_jobs_from_repository=runtime_settings.worker_poll_jobs,
    job_poll_seconds=runtime_settings.worker_job_poll_seconds,
)
