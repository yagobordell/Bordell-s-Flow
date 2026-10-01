"""Idempotent independent YouTube distribution, never audiovisual generation.

A single persistent publication registry is shared by CLI and pipeline.
Canonical state is written before every remote side effect; video IDs are
durably persisted BEFORE optional thumbnail requests. Any uncertainty
about a previous videos.insert fails closed rather than creating a duplicate.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .attempt import UploadAttempt
from .auth import load_credentials
from .client import build_youtube_client
from .errors import (
    YouTubeAmbiguousUploadError,
    YouTubeConfigurationError,
    YouTubeThumbnailError,
    YouTubeValidationError,
)
from .metadata import validate_video
from .models import YouTubePublishRequest, YouTubePublishResult
from .state import (
    PublicationStateStore,
    metadata_digest,
    new_state,
    sha256_file,
)
from .store import YouTubeStore
from .thumbnail import set_thumbnail, validate_thumbnail
from .uploader import upload_video

Uploader = Callable[..., YouTubePublishResult]
ThumbnailSetter = Callable[..., None]


def result_from_state(state: dict[str, Any]) -> YouTubePublishResult:
    video_id = state["video_id"]
    if not isinstance(video_id, str) or not video_id:
        raise YouTubeAmbiguousUploadError("Published video ID is missing from the state")
    return YouTubePublishResult(
        video_id=video_id,
        url="https://www.youtube.com/watch?v=" + video_id,
        privacy=state["privacy"],
        channel=state["channel"],
        thumbnail_uploaded=state["thumbnail_status"] == "uploaded",
    )


def publish_to_youtube(
    request: YouTubePublishRequest,
    *,
    keystore_dir: Path,
    state_dir: Path,
    state_path: Path | None = None,
    youtube: Any | None = None,
    client_factory: Callable[[], Any] | None = None,
    upload_fn: Uploader = upload_video,
    thumbnail_fn: ThumbnailSetter = set_thumbnail,
    progress: Callable[[int], None] | None = None,
    retry_failed_safe: bool = False,
) -> YouTubePublishResult:
    """Create at most one video for (channel, MP4 hash) per shared state root.

    CLI may supply fake youtube/upload_fn/thumbnail_fn for offline tests.
    Never connect automatically when YOUTUBE_ENABLED is disabled: the caller
    controls activation; this library also serves deliberate manual uploads.
    """
    video, _ = validate_video(request.video_path)
    video_sha = sha256_file(video)
    metadata_sha = metadata_digest(request)
    thumbnail_sha: str | None = None
    if request.thumbnail_path is not None:
        image, _mimetype, _size = validate_thumbnail(request.thumbnail_path)
        thumbnail_sha = sha256_file(image)
    registry = PublicationStateStore(state_dir)
    client = youtube

    def get_client() -> Any:
        nonlocal client
        if client is None:
            if client_factory is not None:
                client = client_factory()
            else:
                store = YouTubeStore(keystore_dir)
                credentials = load_credentials(request.channel, store)
                client = build_youtube_client(credentials)
        return client

    with registry.locked(request.channel, video_sha):
        state = registry.read(request.channel, video_sha)
        if state is None:
            # Import a phase-3 receipt only for the SAME original file and
            # exact normalized metadata. Never silently dismiss ambiguity.
            previous = UploadAttempt(request)
            previous.fingerprint()
            legacy = previous.existing_success()
            state = new_state(request, video_sha, metadata_sha, thumbnail_sha)
            if legacy is not None:
                state["status"] = "uploaded"
                state["video_id"] = legacy.video_id
                state["url"] = legacy.url
                state["legacy_receipt_migrated"] = True
            registry.write(state, mirror=state_path)
        else:
            if state["metadata_sha256"] != metadata_sha:
                raise YouTubeValidationError(
                    "This video is already registered for the channel with different "
                    "metadata. Never re-upload it: reconcile the existing publication."
                )
            if state["status"] in ("uploading", "ambiguous"):
                raise YouTubeAmbiguousUploadError(
                    "Previous upload is uncertain; inspect YouTube Studio and reconcile "
                    "the shared publication state before another videos.insert."
                )
            if state["status"] == "failed_safe":
                if not retry_failed_safe:
                    raise YouTubeValidationError(
                        "Explicit --retry-failed-safe is required for this recorded attempt"
                    )
                if state.get("video_id") is not None:
                    raise YouTubeAmbiguousUploadError("A failed-safe attempt contains a video ID")
                state["status"] = "pending"
                registry.write(state, mirror=state_path)

        if state.get("video_id") is None:
            if state["status"] != "pending":
                raise YouTubeAmbiguousUploadError(
                    "No confirmed video ID is available; cannot start another upload"
                )
            youtube_client = get_client()

            def started() -> None:
                if sha256_file(video) != video_sha:
                    raise YouTubeValidationError("MP4 changed before uploading")
                state["status"] = "uploading"
                registry.write(state, mirror=state_path)

            def completed(video_id: str) -> None:
                state["video_id"] = video_id
                state["url"] = "https://www.youtube.com/watch?v=" + video_id
                state["status"] = "uploaded"
                state["last_http_status"] = None
                state["last_api_reason"] = None
                registry.write(state, mirror=state_path)

            def failed(code: int | None, reason: str | None) -> None:
                state["status"] = "ambiguous"
                state["last_http_status"] = code
                state["last_api_reason"] = reason
                registry.write(state, mirror=state_path)

            upload_fn(
                youtube_client,
                request,
                on_started=started,
                on_completed=completed,
                on_failure=failed,
                progress=progress,
            )
            if not state.get("video_id"):
                raise YouTubeAmbiguousUploadError(
                    "Upload did not produce a durable video ID; inspect YouTube Studio"
                )

        # A known video_id must NEVER trigger videos.insert again.
        if request.thumbnail_path is not None:
            if (
                state["thumbnail_status"] != "uploaded"
                or state.get("thumbnail_sha256") != thumbnail_sha
            ):
                state["status"] = "uploaded"
                state["thumbnail_status"] = "uploading"
                state["thumbnail_sha256"] = thumbnail_sha
                registry.write(state, mirror=state_path)
                try:
                    thumbnail_fn(get_client(), state["video_id"], request.thumbnail_path)
                except YouTubeThumbnailError:
                    state["status"] = "uploaded"
                    state["thumbnail_status"] = "failed"
                    registry.write(state, mirror=state_path)
                    raise
                except Exception:
                    state["status"] = "uploaded"
                    state["thumbnail_status"] = "failed"
                    registry.write(state, mirror=state_path)
                    raise YouTubeThumbnailError(
                        "Thumbnail operation failed; video remains safely uploaded "
                        "and only its thumbnail may be retried"
                    ) from None
                state["thumbnail_status"] = "uploaded"
                registry.write(state, mirror=state_path)
        elif state["thumbnail_status"] in ("pending", "failed", "uploading"):
            # No new thumbnail was supplied: do not claim an old attempt succeeded.
            state["thumbnail_status"] = "not_requested"
            state["thumbnail_sha256"] = None

        state["status"] = "completed"
        state["completed_at"] = datetime.now(UTC).isoformat()
        registry.write(state, mirror=state_path)
        return result_from_state(state)


def default_state_dir(keystore_dir: Path, override: Path | None = None) -> Path:
    """One shared external registry by default, across local git worktrees."""
    if override is not None:
        selected = Path(override).expanduser()
        if not selected.is_absolute():
            raise YouTubeConfigurationError(
                "YOUTUBE_STATE_DIR must be an absolute shared path across worktrees"
            )
        return selected
    keystore = Path(keystore_dir).expanduser()
    if not keystore.is_absolute():
        raise YouTubeConfigurationError("Absolute YOUTUBE_KEYSTORE_DIR required")
    return keystore.parent / "youtube-publications"
