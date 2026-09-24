from __future__ import annotations

from .repository import InMemoryJobRepository, PostgresJobRepository
from .settings import InferenceWorkerSettings
from .storage import LocalObjectStorage, R2ObjectStorage
from .tasks import TaskRunnerRegistry
from .worker import InferenceWorker


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise RuntimeError(f"validated production setting unexpectedly missing: {name}")
    return value


def build_worker(
    settings: InferenceWorkerSettings,
    *,
    runners: TaskRunnerRegistry,
) -> InferenceWorker:
    if settings.worker_mode == "local":
        storage = LocalObjectStorage(settings.local_object_root)
        repository = InMemoryJobRepository()
    else:
        endpoint_url = _required(settings.r2_endpoint_url, "r2_endpoint_url")
        bucket = _required(settings.r2_bucket, "r2_bucket")
        access_key_id = _required(settings.r2_access_key_id, "r2_access_key_id")
        secret_access_key = _required(settings.r2_secret_access_key, "r2_secret_access_key")
        postgres_dsn = _required(settings.postgres_dsn, "postgres_dsn")
        storage = R2ObjectStorage.create(
            endpoint_url=endpoint_url,
            bucket=bucket,
            access_key_id=access_key_id.get_secret_value(),
            secret_access_key=secret_access_key.get_secret_value(),
        )
        repository = PostgresJobRepository(
            postgres_dsn.get_secret_value(),
            max_connections=settings.worker_max_db_connections,
        )

    return InferenceWorker(
        storage=storage,
        repository=repository,
        runners=runners,
        worker_id=settings.worker_id,
        temp_dir=settings.worker_temp_dir,
        lease_seconds=settings.worker_lease_seconds,
        heartbeat_seconds=settings.worker_heartbeat_seconds,
        gpu_retry_cooldown_seconds=settings.worker_gpu_retry_cooldown_seconds,
    )
