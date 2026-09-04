from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GPUWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gpu_worker_mode: Literal["production", "local"] = "production"
    gpu_worker_runtime: Literal["phase7", "phase8"] = "phase7"
    gpu_worker_id: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    gpu_worker_temp_dir: Path = Path("/tmp/ai-video-factory")
    gpu_worker_lease_seconds: int = Field(default=90, ge=30, le=3600)
    gpu_worker_heartbeat_seconds: int = Field(default=30, ge=5, le=1200)
    gpu_worker_max_db_connections: int = Field(default=4, ge=1, le=32)
    local_object_root: Path = Path("data/phase7-local")

    ltx_model_root: Path = Path("/workspace/models/ltx-2.5")
    ltx_device: str = "cuda"

    postgres_dsn: SecretStr | None = None
    r2_endpoint_url: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if self.gpu_worker_heartbeat_seconds >= self.gpu_worker_lease_seconds:
            raise ValueError("GPU_WORKER_HEARTBEAT_SECONDS must be shorter than the lease")
        if not self.ltx_device.strip():
            raise ValueError("LTX_DEVICE must be non-empty")
        if self.gpu_worker_mode == "production":
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
                    "production GPU worker configuration is missing: " + ", ".join(missing)
                )
        return self
