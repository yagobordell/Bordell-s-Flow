from __future__ import annotations

from pathlib import Path

from botocore.exceptions import ClientError

from ai_video_factory.inference.storage import R2ObjectStorage


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.bucket_pinged = False

    def upload_file(
        self,
        source: str,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, object],
    ) -> None:
        self.objects[key] = {
            "body": Path(source).read_bytes(),
            "bucket": bucket,
            "content_type": ExtraArgs["ContentType"],
            "metadata": ExtraArgs["Metadata"],
        }

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        assert self.objects[key]["bucket"] == bucket
        Path(destination).write_bytes(self.objects[key]["body"])  # type: ignore[arg-type]

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        item = self.objects[Key]
        assert item["bucket"] == Bucket
        return {
            "ContentLength": len(item["body"]),  # type: ignore[arg-type]
            "ContentType": item["content_type"],
            "Metadata": item["metadata"],
            "ETag": '"etag-123"',
        }

    def head_bucket(self, *, Bucket: str) -> None:
        assert Bucket == "bucket"
        self.bucket_pinged = True


def test_r2_adapter_upload_download_stat_and_ping(tmp_path: Path) -> None:
    client = FakeS3Client()
    storage = R2ObjectStorage(client, "bucket")
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")

    uploaded = storage.upload(
        source,
        "jobs/job-1/output.bin",
        content_type="application/octet-stream",
        metadata={"artifact-sha256": "abc"},
    )
    destination = tmp_path / "downloaded.bin"
    downloaded = storage.download("jobs/job-1/output.bin", destination)
    storage.ping()

    assert uploaded.etag == "etag-123"
    assert downloaded.metadata["artifact-sha256"] == "abc"
    assert destination.read_bytes() == b"payload"
    assert client.bucket_pinged is True
    assert storage.stat("missing") is None
