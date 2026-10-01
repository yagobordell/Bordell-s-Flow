"""Minimal phase-3 local upload guard against accidental cross-process duplicates.

The full youtube_publish.json state machine is reserved for phase 5.
This provisional sidecar never starts a second videos.insert if a previous
attempt may have succeeded; manual reconciliation is required if ambiguous.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import YouTubeAmbiguousUploadError, YouTubeValidationError
from .metadata import build_insert_payload
from .models import YouTubePublishRequest, YouTubePublishResult

SCHEMA = "youtube-upload-attempt/v1"


def _digest(path: Path) -> str:
    try:
        before = path.stat()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError("File changed while hashing")
        return digest
    except OSError:
        raise YouTubeValidationError("MP4 could not be fingerprinted consistently") from None


class UploadAttempt:
    """Persist an attempt next to its video, without modifying the MP4 itself."""

    def __init__(self, request: YouTubePublishRequest) -> None:
        self.request = request
        self.path = request.video_path.with_name(
            request.video_path.name + ".youtube-upload-attempt.json"
        )
        part, body = build_insert_payload(request)
        content = {
            "part": part,
            "body": body,
            "notifySubscribers": request.notify_subscribers,
        }
        self.metadata_sha256 = hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.video_sha256: str | None = None
        self.video_bytes: int | None = None
        self.reserved = False

    def fingerprint(self) -> None:
        self.video_sha256 = _digest(self.request.video_path)
        self.video_bytes = self.request.video_path.stat().st_size

    def _read(self) -> dict[str, Any] | None:
        if self.path.is_symlink() or self.path.is_junction():
            raise YouTubeAmbiguousUploadError("Unsafe existing upload attempt record")
        if not self.path.exists():
            return None
        try:
            if not self.path.is_file() or self.path.stat().st_size > 1024 * 1024:
                raise ValueError("Invalid record")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != SCHEMA:
                raise ValueError("Unknown record")
            return data
        except (OSError, UnicodeError, ValueError):
            raise YouTubeAmbiguousUploadError(
                "Unrecognized upload attempt record; inspect it before another upload"
            ) from None

    def existing_success(self) -> YouTubePublishResult | None:
        """Return the exact previous completed upload; refuse every other reuse."""
        old = self._read()
        if old is None:
            return None
        if (
            self.video_sha256 == old.get("video_sha256")
            and self.metadata_sha256 == old.get("metadata_sha256")
            and self.request.channel == old.get("channel")
            and old.get("status") == "completed"
        ):
            video_id = old.get("video_id")
            if isinstance(video_id, str) and video_id and all(
                char.isascii() and (char.isalnum() or char in "_-") for char in video_id
            ):
                return YouTubePublishResult(
                    video_id=video_id,
                    url="https://www.youtube.com/watch?v=" + video_id,
                    privacy=self.request.privacy,
                    channel=self.request.channel,
                    thumbnail_uploaded=False,
                )
        raise YouTubeAmbiguousUploadError(
            "An earlier upload record exists for this MP4 path. Never start another "
            "insert before checking its result on YouTube and reconciling manually."
        )

    def reserve(self) -> None:
        """Atomic exclusive reservation across processes, just before first chunk."""
        if self.video_sha256 is None or self.video_bytes is None:
            raise YouTubeValidationError("Fingerprint the MP4 before uploading")
        if (
            self.request.video_path.stat().st_size != self.video_bytes
            or _digest(self.request.video_path) != self.video_sha256
        ):
            raise YouTubeValidationError("MP4 changed after preflight; upload refused")
        record: dict[str, Any] = {
            "schema_version": SCHEMA,
            "status": "uploading",
            "channel": self.request.channel,
            "video_sha256": self.video_sha256,
            "video_bytes": self.video_bytes,
            "metadata_sha256": self.metadata_sha256,
            "privacy": self.request.privacy,
            "created_at": datetime.now(UTC).isoformat(),
            "video_id": None,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError:
            raise YouTubeAmbiguousUploadError(
                "An upload attempt record already exists or cannot be reserved. "
                "Inspect the previous attempt before continuing."
            ) from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.reserved = True
        except OSError:
            # Nothing has been sent yet, so a partial reservation can be discarded.
            self.path.unlink(missing_ok=True)
            raise YouTubeValidationError("Failed to reserve local upload attempt") from None

    def complete(self, video_id: str) -> None:
        """Write the successful ID immediately; never erase on persistence failure."""
        if not self.reserved or not isinstance(video_id, str) or not video_id:
            raise YouTubeAmbiguousUploadError("Upload result could not be safely persisted")
        record = self._read()
        if record is None or record.get("status") != "uploading":
            raise YouTubeAmbiguousUploadError("Local upload attempt changed unexpectedly")
        record["status"] = "completed"
        record["video_id"] = video_id
        record["completed_at"] = datetime.now(UTC).isoformat()
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".youtube-attempt-", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(name)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.is_symlink() or self.path.is_junction():
                raise OSError("Unsafe replacement")
            os.replace(temporary, self.path)
        except OSError:
            raise YouTubeAmbiguousUploadError(
                "YouTube returned an ID, but saving the local receipt failed. "
                "Inspect the channel and reconcile this attempt; DO NOT re-upload."
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
