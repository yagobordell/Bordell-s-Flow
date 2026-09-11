from __future__ import annotations

import logging

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.runtime import build_worker as build_inference_worker
from ai_video_factory.inference.settings import InferenceWorkerSettings
from ai_video_factory.inference.worker import InferenceWorker

from .ltx_video import DirectLTX25Backend, LTXVideoTaskRunner
from .settings import GPUWorkerSettings
from .tasks import TaskRunnerRegistry


def _build_runners(settings: GPUWorkerSettings) -> TaskRunnerRegistry:
    if settings.gpu_worker_runtime == "phase7":
        return TaskRunnerRegistry.phase7()

    backend = DirectLTX25Backend(
        model_root=settings.ltx_model_root,
        device=settings.ltx_device,
    )
    return TaskRunnerRegistry.phase8(LTXVideoTaskRunner(backend=backend))


def _shared_settings(settings: GPUWorkerSettings) -> InferenceWorkerSettings:
    """Adapt legacy GPU environment names to the shared inference worker core."""

    return InferenceWorkerSettings(
        _env_file=None,
        worker_mode=settings.gpu_worker_mode,
        worker_id=settings.gpu_worker_id,
        worker_temp_dir=settings.gpu_worker_temp_dir,
        worker_lease_seconds=settings.gpu_worker_lease_seconds,
        worker_heartbeat_seconds=settings.gpu_worker_heartbeat_seconds,
        worker_max_db_connections=settings.gpu_worker_max_db_connections,
        local_object_root=settings.local_object_root,
        postgres_dsn=settings.postgres_dsn,
        r2_endpoint_url=settings.r2_endpoint_url,
        r2_bucket=settings.r2_bucket,
        r2_access_key_id=settings.r2_access_key_id,
        r2_secret_access_key=settings.r2_secret_access_key,
    )


def build_worker(settings: GPUWorkerSettings) -> InferenceWorker:
    return build_inference_worker(
        _shared_settings(settings),
        runners=_build_runners(settings),
    )


runtime_settings = GPUWorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(
    build_worker(runtime_settings),
    prepare_in_background=runtime_settings.gpu_worker_runtime == "phase8",
)
