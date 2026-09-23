from __future__ import annotations

import boto3
from scripts.pipeline.r2_client import create_r2_storage


def test_local_r2_client_uses_fast_fail_network_defaults(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_client(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(boto3, "client", fake_client)

    storage = create_r2_storage(
        endpoint_url="https://example.r2.cloudflarestorage.com",
        bucket="bucket",
        access_key_id="key",
        secret_access_key="secret",
    )

    config = captured["config"]
    assert config.connect_timeout == 5.0
    assert config.read_timeout == 30.0
    assert config.max_pool_connections == 32
    assert config.retries == {"total_max_attempts": 3, "mode": "standard"}
    assert config.tcp_keepalive is True
    assert storage.bucket == "bucket"


def test_local_r2_client_allows_positive_environment_overrides(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("R2_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("R2_READ_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("R2_TOTAL_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("R2_MAX_POOL_CONNECTIONS", "8")

    create_r2_storage(
        endpoint_url="https://example.r2.cloudflarestorage.com",
        bucket="bucket",
        access_key_id="key",
        secret_access_key="secret",
    )

    config = captured["config"]
    assert config.connect_timeout == 2.0
    assert config.read_timeout == 15.0
    assert config.max_pool_connections == 8
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
