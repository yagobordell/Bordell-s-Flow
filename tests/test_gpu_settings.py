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
    assert settings.gpu_worker_runtime == "phase7"


def test_phase8_runtime_defaults_to_validated_model_location_and_cuda() -> None:
    settings = GPUWorkerSettings(
        _env_file=None,
        gpu_worker_mode="local",
        gpu_worker_runtime="phase8",
        gpu_worker_lease_seconds=60,
        gpu_worker_heartbeat_seconds=10,
    )

    assert settings.gpu_worker_runtime == "phase8"
    assert settings.ltx_model_root.as_posix() == "/workspace/models/ltx-2.5"
    assert settings.ltx_device == "cuda"


def test_production_settings_require_cloud_secrets() -> None:
    with pytest.raises(ValidationError, match="production GPU worker configuration is missing"):
        GPUWorkerSettings(_env_file=None, gpu_worker_mode="production")


def test_production_settings_reject_blank_cloud_secrets() -> None:
    with pytest.raises(ValidationError, match="postgres_dsn"):
        GPUWorkerSettings(
            _env_file=None,
            gpu_worker_mode="production",
            postgres_dsn="",
            r2_endpoint_url="",
            r2_bucket="",
            r2_access_key_id="",
            r2_secret_access_key="",
        )


def test_heartbeat_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValidationError, match="must be shorter"):
        GPUWorkerSettings(
            _env_file=None,
            gpu_worker_mode="local",
            gpu_worker_lease_seconds=30,
            gpu_worker_heartbeat_seconds=30,
        )


def test_ltx_device_must_be_non_empty() -> None:
    with pytest.raises(ValidationError, match="LTX_DEVICE must be non-empty"):
        GPUWorkerSettings(
            _env_file=None,
            gpu_worker_mode="local",
            ltx_device="   ",
        )
