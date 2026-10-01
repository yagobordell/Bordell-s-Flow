"""Durable single-host YouTube publication registry and atomic per-run snapshots.

All processes publishing with one keystore MUST share the same state root.
The registry key is normalized channel + content digest, never a filename.
Existing ambiguous attempts always fail closed, including after crashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import (
    YouTubeAmbiguousUploadError,
    YouTubeConfigurationError,
    YouTubeValidationError,
)
from .locking import channel_lock
from .models import YouTubePublishRequest

SCHEMA = "youtube-publish-state/v1"
STATUSES = frozenset({
    "pending", "uploading", "uploaded", "completed", "failed_safe", "ambiguous",
})
THUMB_STATUSES = frozenset({
    "not_requested", "pending", "uploading", "uploaded", "failed",
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
MAX_STATE_BYTES = 1024 * 1024


def sha256_file(file: Path) -> str:
    """Hash a regular file, detecting common concurrent modifications."""
    if file.is_symlink() or file.is_junction() or not file.is_file():
        raise YouTubeValidationError("Publication input is missing or linked")
    try:
        before = file.stat()
        with file.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = file.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError("File changed during hashing")
        return digest
    except OSError:
        raise YouTubeValidationError("Publication input changed during hashing") from None


def metadata_digest(request: YouTubePublishRequest) -> str:
    """Thumbnail is independently mutable; omit it from video metadata identity."""
    from .metadata import build_insert_payload

    part, body = build_insert_payload(request)
    data = {
        "part": part, "body": body, "notifySubscribers": request.notify_subscribers,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _check_ancestors(path: Path) -> None:
    for segment in (path, *path.parents):
        if segment.is_symlink() or segment.is_junction():
            raise YouTubeConfigurationError("YouTube state paths cannot traverse filesystem links")


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Atomic durable replace, with link and size protections."""
    _check_ancestors(path)
    if not path.parent.is_dir():
        raise YouTubeConfigurationError("YouTube state destination directory is missing")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".youtube-state-", suffix=".tmp", dir=path.parent,
        )
        temporary = Path(name)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.is_junction():
            raise OSError("Linked state destination")
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, TypeError, ValueError):
        raise YouTubeConfigurationError(
            "Cannot persist YouTube publication state; check the shared state folder"
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class PublicationStateStore:
    """A durable local registry; interprocess lock covers the entire upload."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().absolute()
        _check_ancestors(self.root)

    def ensure(self) -> None:
        _check_ancestors(self.root)
        if self.root.exists() and not self.root.is_dir():
            raise YouTubeConfigurationError("YouTube state root is not a directory")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise YouTubeConfigurationError("Could not create shared YouTube state root") from None

    @staticmethod
    def key(channel: str, video_sha256: str) -> str:
        if _DIGEST.fullmatch(video_sha256) is None:
            raise YouTubeValidationError("Invalid content fingerprint")
        normalized = unicodedata.normalize("NFC", channel).casefold()
        return hashlib.sha256(
            (normalized + "\0" + video_sha256).encode("utf-8")
        ).hexdigest()

    def path_for(self, channel: str, video_sha256: str) -> Path:
        return self.root / (self.key(channel, video_sha256) + ".json")

    @contextmanager
    def locked(self, channel: str, video_sha256: str) -> Iterator[None]:
        self.ensure()
        # Reuse the crash-released POSIX/Windows OS lock from desktop OAuth.
        # Unlike the OAuth lock, the key is content-specific and in state_root.
        with channel_lock(self.root, self.key(channel, video_sha256), timeout=15.0):
            yield

    def read(self, channel: str, video_sha256: str) -> dict[str, Any] | None:
        path = self.path_for(channel, video_sha256)
        _check_ancestors(path)
        if not path.exists():
            return None
        try:
            if not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("invalid")
            state = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(state, dict) or state.get("schema_version") != SCHEMA
                or state.get("status") not in STATUSES
                or state.get("thumbnail_status") not in THUMB_STATUSES
                or state.get("video_sha256") != video_sha256
                or not isinstance(state.get("channel"), str)
                or unicodedata.normalize("NFC", state["channel"]).casefold()
                != unicodedata.normalize("NFC", channel).casefold()
                or not isinstance(state.get("metadata_sha256"), str)
                or _DIGEST.fullmatch(state["metadata_sha256"]) is None
            ):
                raise ValueError("invalid")
            video_id = state.get("video_id")
            if video_id is not None and (
                not isinstance(video_id, str) or _VIDEO_ID.fullmatch(video_id) is None
            ):
                raise ValueError("invalid")
            if state["status"] in ("uploaded", "completed") and not video_id:
                raise ValueError("invalid")
            if state["status"] in ("pending", "uploading") and video_id:
                raise ValueError("invalid")
            return state
        except (OSError, UnicodeError, ValueError, TypeError):
            raise YouTubeAmbiguousUploadError(
                "Unrecognized publication state: inspect and reconcile before any upload"
            ) from None

    def write(self, state: dict[str, Any], *, mirror: Path | None = None) -> None:
        """Canonical registry is always written FIRST, then optional per-run snapshot."""
        if (
            state.get("schema_version") != SCHEMA
            or state.get("status") not in STATUSES
            or state.get("thumbnail_status") not in THUMB_STATUSES
        ):
            raise YouTubeConfigurationError("Refusing invalid YouTube publication state")
        _write_json(self.path_for(state["channel"], state["video_sha256"]), state)
        if mirror is not None:
            _write_json(mirror, state)

    def snapshot(self, state: dict[str, Any], path: Path | None) -> None:
        if path is not None:
            _write_json(path, state)


def new_state(
    request: YouTubePublishRequest,
    video_sha256: str,
    metadata_sha256: str,
    thumbnail_sha256: str | None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": SCHEMA,
        "status": "pending",
        "channel": request.channel,
        "video_sha256": video_sha256,
        "metadata_sha256": metadata_sha256,
        "video_id": None,
        "url": None,
        "privacy": request.privacy,
        "thumbnail_status": "pending" if thumbnail_sha256 else "not_requested",
        "thumbnail_sha256": thumbnail_sha256,
        "started_at": now,
        "completed_at": None,
        "last_http_status": None,
        "last_api_reason": None,
    }
