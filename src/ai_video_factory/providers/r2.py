from __future__ import annotations

import os

from botocore.config import Config

from ai_video_factory.inference.storage import R2ObjectStorage

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 30.0
_DEFAULT_TOTAL_MAX_ATTEMPTS = 3
_DEFAULT_MAX_POOL_CONNECTIONS = 32


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def create_r2_storage(
    *,
    endpoint_url: str,
    bucket: str,
    access_key_id: str,
    secret_access_key: str,
) -> R2ObjectStorage:
    """Create an R2 object-storage client with bounded network waits."""

    import boto3

    config = Config(
        connect_timeout=_positive_float(
            "R2_CONNECT_TIMEOUT_SECONDS",
            _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        ),
        read_timeout=_positive_float(
            "R2_READ_TIMEOUT_SECONDS",
            _DEFAULT_READ_TIMEOUT_SECONDS,
        ),
        max_pool_connections=_positive_int(
            "R2_MAX_POOL_CONNECTIONS",
            _DEFAULT_MAX_POOL_CONNECTIONS,
        ),
        retries={
            "total_max_attempts": _positive_int(
                "R2_TOTAL_MAX_ATTEMPTS",
                _DEFAULT_TOTAL_MAX_ATTEMPTS,
            ),
            "mode": "standard",
        },
        tcp_keepalive=True,
    )
    client = boto3.client(
        service_name="s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=config,
    )
    return R2ObjectStorage(client, bucket)
