from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker

from .settings import LTX25WorkerSettings
from .worker_factory import build_ltx25_runners


def build_ltx25_worker(settings: LTX25WorkerSettings):
    return build_worker(settings, runners=build_ltx25_runners(settings))


runtime_settings = LTX25WorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_ltx25_worker(runtime_settings),
    prepare_in_background=True,
    poll_jobs_from_repository=runtime_settings.worker_poll_jobs,
    job_poll_seconds=runtime_settings.worker_job_poll_seconds,
)
