from __future__ import annotations

import boto3

from ai_video_factory.inference.storage import R2ObjectStorage
from ai_video_factory.providers.r2 import create_r2_storage


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



def test_r2_signed_urls_and_delete_use_bucket_and_key() -> None:
    calls: list[tuple[str, object]] = []

    class Client:
        def generate_presigned_url(self, operation, *, Params, ExpiresIn):
            calls.append(
                (
                    "presign",
                    {
                        "operation": operation,
                        "params": Params,
                        "expires": ExpiresIn,
                    },
                )
            )
            return f"https://signed.example/{operation}"

        def delete_object(self, *, Bucket, Key):
            calls.append(("delete", {"bucket": Bucket, "key": Key}))

    storage = R2ObjectStorage(Client(), "bucket")

    assert storage.presign_get("input.png", expires_seconds=900) == (
        "https://signed.example/get_object"
    )
    assert storage.presign_put("output.png", expires_seconds=1200) == (
        "https://signed.example/put_object"
    )
    storage.delete("temporary.png")

    assert calls == [
        (
            "presign",
            {
                "operation": "get_object",
                "params": {"Bucket": "bucket", "Key": "input.png"},
                "expires": 900,
            },
        ),
        (
            "presign",
            {
                "operation": "put_object",
                "params": {"Bucket": "bucket", "Key": "output.png"},
                "expires": 1200,
            },
        ),
        ("delete", {"bucket": "bucket", "key": "temporary.png"}),
    ]
