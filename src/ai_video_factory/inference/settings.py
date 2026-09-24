from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class InferenceWorkerSettings(BaseSettings):
    """Shared worker infrastructure settings, independent of any model runtime."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="INFERENCE_",
        populate_by_name=True,
        extra="ignore",
    )

    worker_mode: Literal["production", "local"] = "production"
    worker_id: str = Field(default_factory=_default_worker_id)
    salad_instance_id: str | None = Field(default=None, validation_alias="SALAD_INSTANCE_ID")
    worker_temp_dir: Path = Path("/tmp/ai-video-factory")
    worker_lease_seconds: int = Field(default=90, ge=30, le=3600)
    worker_heartbeat_seconds: int = Field(default=30, ge=5, le=1200)
    worker_max_db_connections: int = Field(default=4, ge=1, le=32)
    worker_poll_jobs: bool = False
    worker_job_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    worker_gpu_retry_cooldown_seconds: float = Field(default=60.0, ge=0, le=3600)
    local_object_root: Path = Path("data/inference-local")

    postgres_dsn: SecretStr | None = Field(default=None, validation_alias="POSTGRES_DSN")
    r2_endpoint_url: str | None = Field(default=None, validation_alias="R2_ENDPOINT_URL")
    r2_bucket: str | None = Field(default=None, validation_alias="R2_BUCKET")
    r2_access_key_id: SecretStr | None = Field(default=None, validation_alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: SecretStr | None = Field(
        default=None,
        validation_alias="R2_SECRET_ACCESS_KEY",
    )

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        salad_instance_id = str(self.salad_instance_id or "").strip()
        if salad_instance_id:
            self.worker_id = f"{self.worker_id}-{salad_instance_id}"
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("INFERENCE_WORKER_HEARTBEAT_SECONDS must be shorter than the lease")
        if self.worker_mode == "production":
            missing = []
            for name in (
                "postgres_dsn",
                "r2_endpoint_url",
                "r2_bucket",
                "r2_access_key_id",
                "r2_secret_access_key",
            ):
                value = getattr(self, name)
                if isinstance(value, SecretStr):
                    value = value.get_secret_value()
                if value is None or not str(value).strip():
                    missing.append(name)
            if missing:
                raise ValueError(
                    "production inference worker configuration is missing: " + ", ".join(missing)
                )
        return self
