from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from botocore.exceptions import ClientError

from .ports import ObjectCreateResult, StoredObject


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class R2ObjectStorage:
    """Cloudflare R2 adapter using its S3-compatible API."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self.bucket = bucket

    @classmethod
    def create(
        cls,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> R2ObjectStorage:
        import boto3

        client = boto3.client(
            service_name="s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )
        return cls(client, bucket)

    def download(self, key: str, destination: Path) -> StoredObject:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, key, str(destination))
        stored = self.stat(key)
        if stored is None:  # pragma: no cover - defensive race protection
            raise FileNotFoundError(f"object disappeared after download: {key}")
        return stored

    def upload(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> StoredObject:
        self._client.upload_file(
            str(source),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "Metadata": dict(metadata)},
        )
        stored = self.stat(key)
        if stored is None:  # pragma: no cover - defensive provider protection
            raise RuntimeError(f"uploaded object cannot be read back: {key}")
        return stored

    def create_if_absent(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> ObjectCreateResult:
        created = False
        try:
            with source.open("rb") as stream:
                self._client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=stream,
                    ContentType=content_type,
                    Metadata=dict(metadata),
                    IfNoneMatch="*",
                )
            created = True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code != "PreconditionFailed" and status != 412:
                raise

        stored = self.stat(key)
        if stored is None:  # pragma: no cover - defensive provider protection
            raise RuntimeError(f"create-only object cannot be read back: {key}")
        return ObjectCreateResult(stored=stored, created=created)

    def stat(self, key: str) -> StoredObject | None:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return None
            raise

        return StoredObject(
            key=key,
            content_type=response.get("ContentType", "application/octet-stream"),
            size_bytes=int(response["ContentLength"]),
            etag=str(response.get("ETag", "")).strip('"') or None,
            metadata={str(k).lower(): str(v) for k, v in response.get("Metadata", {}).items()},
        )

    def ping(self) -> None:
        self._client.head_bucket(Bucket=self.bucket)


class LocalObjectStorage:
    """Filesystem implementation for tests and local development."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._metadata_root = self.root / ".metadata"

    def _path(self, key: str) -> Path:
        candidate = (self.root / PurePosixPath(key)).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("object key escapes local object root")
        return candidate

    def _metadata_path(self, key: str) -> Path:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._metadata_root / f"{name}.json"

    def download(self, key: str, destination: Path) -> StoredObject:
        source = self._path(key)
        if not source.is_file():
            raise FileNotFoundError(f"local object not found: {key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        stored = self.stat(key)
        if stored is None:  # pragma: no cover - defensive local race protection
            raise FileNotFoundError(f"local object disappeared after download: {key}")
        return stored

    def upload(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> StoredObject:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)

        self._metadata_root.mkdir(parents=True, exist_ok=True)
        metadata_path = self._metadata_path(key)
        metadata_temporary = metadata_path.with_suffix(".tmp")
        document = {"content_type": content_type, "metadata": dict(metadata)}
        metadata_temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        os.replace(metadata_temporary, metadata_path)

        stored = self.stat(key)
        if stored is None:  # pragma: no cover - defensive local filesystem protection
            raise RuntimeError(f"uploaded local object cannot be read back: {key}")
        return stored

    def create_if_absent(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> ObjectCreateResult:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with destination.open("xb") as target, source.open("rb") as stream:
                shutil.copyfileobj(stream, target)
            created = True
        except FileExistsError:
            pass
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        if created:
            self._metadata_root.mkdir(parents=True, exist_ok=True)
            metadata_path = self._metadata_path(key)
            metadata_temporary = metadata_path.with_suffix(".tmp")
            document = {"content_type": content_type, "metadata": dict(metadata)}
            metadata_temporary.write_text(
                json.dumps(document, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(metadata_temporary, metadata_path)

        stored = self.stat(key)
        if stored is None:  # pragma: no cover - defensive local protection
            raise RuntimeError(f"create-only local object cannot be read back: {key}")
        return ObjectCreateResult(stored=stored, created=created)

    def stat(self, key: str) -> StoredObject | None:
        path = self._path(key)
        if not path.is_file():
            return None

        document: dict[str, Any] = {}
        metadata_path = self._metadata_path(key)
        if metadata_path.is_file():
            document = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = {str(k).lower(): str(v) for k, v in document.get("metadata", {}).items()}
        digest = sha256_file(path)
        return StoredObject(
            key=key,
            content_type=document.get("content_type")
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
            size_bytes=path.stat().st_size,
            etag=digest,
            metadata=metadata,
        )

    def ping(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._metadata_root.mkdir(parents=True, exist_ok=True)
