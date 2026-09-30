from __future__ import annotations

import pytest

from ai_video_factory.inference.storage import R2ObjectStorage


class FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.deleted: list[dict[str, str]] = []

    def generate_presigned_url(self, operation: str, **kwargs) -> str:
        self.calls.append((operation, kwargs))
        return f"https://r2.test/{operation}"

    def delete_object(self, **kwargs) -> None:
        self.deleted.append(kwargs)


def test_r2_generates_signed_get_and_put_urls_and_deletes_objects() -> None:
    client = FakeS3Client()
    storage = R2ObjectStorage(client, "bucket")

    assert storage.presign_get("input.png", expires_seconds=7200) == "https://r2.test/get_object"
    assert storage.presign_put("output.png", expires_seconds=3600) == "https://r2.test/put_object"
    storage.delete("temporary.png")

    assert client.calls == [
        (
            "get_object",
            {"Params": {"Bucket": "bucket", "Key": "input.png"}, "ExpiresIn": 7200},
        ),
        (
            "put_object",
            {"Params": {"Bucket": "bucket", "Key": "output.png"}, "ExpiresIn": 3600},
        ),
    ]
    assert client.deleted == [{"Bucket": "bucket", "Key": "temporary.png"}]


@pytest.mark.parametrize("method", ["presign_get", "presign_put"])
def test_r2_rejects_non_positive_signed_url_expiry(method: str) -> None:
    storage = R2ObjectStorage(FakeS3Client(), "bucket")

    with pytest.raises(ValueError, match="expires_seconds"):
        getattr(storage, method)("key", expires_seconds=0)
