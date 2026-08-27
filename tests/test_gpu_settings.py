import pytest
from pydantic import ValidationError

from ai_video_factory.gpu.settings import GPUWorkerSettings


def test_local_settings_do_not_require_cloud_secrets() -> None:
    settings = GPUWorkerSettings(
        _env_file=None,
        gpu_worker_mode="local",
        gpu_worker_lease_seconds=60,
        gpu_worker_heartbeat_seconds=10,
    )

    assert settings.gpu_worker_mode == "local"


def test_production_settings_require_cloud_secrets() -> None:
    with pytest.raises(ValidationError, match="production GPU worker configuration is missing"):
        GPUWorkerSettings(_env_file=None, gpu_worker_mode="production")


def test_heartbeat_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValidationError, match="must be shorter"):
        GPUWorkerSettings(
            _env_file=None,
            gpu_worker_mode="local",
            gpu_worker_lease_seconds=30,
            gpu_worker_heartbeat_seconds=30,
        )
