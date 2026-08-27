from __future__ import annotations

import logging

from .app import create_app
from .repository import InMemoryJobRepository, PostgresJobRepository
from .settings import GPUWorkerSettings
from .storage import LocalObjectStorage, R2ObjectStorage
from .tasks import TaskRunnerRegistry
from .worker import GPUWorker


def build_worker(settings: GPUWorkerSettings) -> GPUWorker:
    if settings.gpu_worker_mode == "local":
        storage = LocalObjectStorage(settings.local_object_root)
        repository = InMemoryJobRepository()
    else:
        assert settings.r2_endpoint_url is not None
        assert settings.r2_bucket is not None
        assert settings.r2_access_key_id is not None
        assert settings.r2_secret_access_key is not None
        assert settings.postgres_dsn is not None
        storage = R2ObjectStorage.create(
            endpoint_url=settings.r2_endpoint_url,
            bucket=settings.r2_bucket,
            access_key_id=settings.r2_access_key_id.get_secret_value(),
            secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        )
        repository = PostgresJobRepository(
            settings.postgres_dsn.get_secret_value(),
            max_connections=settings.gpu_worker_max_db_connections,
        )

    return GPUWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry.phase7(),
        worker_id=settings.gpu_worker_id,
        temp_dir=settings.gpu_worker_temp_dir,
        lease_seconds=settings.gpu_worker_lease_seconds,
        heartbeat_seconds=settings.gpu_worker_heartbeat_seconds,
    )


runtime_settings = GPUWorkerSettings()
logging.basicConfig(level=logging.INFO)
app = create_app(build_worker(runtime_settings))
