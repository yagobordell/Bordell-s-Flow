from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker
from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .model import (
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_REFERENCE_TASK,
    QwenImage21Backend,
    QwenImage21ImageTaskRunner,
)
from .settings import QwenImage21WorkerSettings


def build_qwen_image_21_worker(settings: QwenImage21WorkerSettings):
    backend = QwenImage21Backend(model_root=settings.model_root, device=settings.device)
    runners = TaskRunnerRegistry(
        [
            QwenImage21ImageTaskRunner(backend=backend, task_name=QWEN_IMAGE_21_REFERENCE_TASK),
            QwenImage21ImageTaskRunner(backend=backend, task_name=QWEN_IMAGE_21_KEYFRAME_TASK),
        ]
    )
    return build_worker(settings, runners=runners)


runtime_settings = QwenImage21WorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_qwen_image_21_worker(runtime_settings),
    prepare_in_background=True,
    poll_jobs_from_repository=runtime_settings.worker_poll_jobs,
    job_poll_seconds=runtime_settings.worker_job_poll_seconds,
)
