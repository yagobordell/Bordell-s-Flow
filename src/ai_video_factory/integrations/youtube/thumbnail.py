"""Optional custom thumbnails: strict local checks and independent API retries.

This module never calls videos.insert. The publisher must persist video_id
before invoking set_thumbnail so a failed thumbnail cannot duplicate a video.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .errors import YouTubeConfigurationError, YouTubeThumbnailError, YouTubeValidationError
from .uploader import _error_policy

MAX_THUMBNAIL_BYTES = 50_000_000
MAX_THUMBNAIL_PIXELS = 100_000_000
_MAX_RETRY_FAILURES = 5
_IMAGE_TYPES = {".png": ("PNG", "image/png"), ".jpg": ("JPEG", "image/jpeg"),
                ".jpeg": ("JPEG", "image/jpeg")}


def validate_thumbnail(path: Path) -> tuple[Path, str, int]:
    """Verify a real, modest-size JPEG/PNG; extension alone is never sufficient."""
    file = Path(path).absolute()
    if file.is_symlink() or file.is_junction() or not file.is_file():
        raise YouTubeValidationError("Thumbnail must be an existing regular image, not a link")
    expected = _IMAGE_TYPES.get(file.suffix.casefold())
    if expected is None:
        raise YouTubeValidationError("YouTube thumbnail must use JPEG or PNG format")
    try:
        size = file.stat().st_size
        if not 0 < size <= MAX_THUMBNAIL_BYTES:
            raise ValueError("invalid size")
        with Image.open(file) as image:
            if image.format != expected[0] or image.width * image.height > MAX_THUMBNAIL_PIXELS:
                raise ValueError("wrong format or oversized image")
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise YouTubeValidationError(
            "Invalid JPEG/PNG thumbnail: corrupt, oversized or mismatched extension"
        ) from None
    return file, expected[1], size


def set_thumbnail(
    youtube: Any,
    video_id: str,
    thumbnail_path: Path,
    *,
    media_factory: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    max_failures: int = _MAX_RETRY_FAILURES,
) -> None:
    """Retry ONLY thumbnails.set; never retry or initiate videos.insert."""
    if (
        not isinstance(video_id, str) or not video_id
        or not all(char.isascii() and (char.isalnum() or char in "_-") for char in video_id)
    ):
        raise YouTubeValidationError("Cannot set thumbnail without a verified video ID")
    if max_failures < 1:
        raise ValueError("max_failures must be positive")
    file, mimetype, _ = validate_thumbnail(thumbnail_path)
    if media_factory is None:
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError:
            raise YouTubeConfigurationError(
                "Install optional YouTube dependencies: uv sync --locked --extra youtube"
            ) from None
        media_factory = MediaFileUpload
    try:
        media = media_factory(filename=str(file), mimetype=mimetype, resumable=False)
    except Exception:
        raise YouTubeThumbnailError(
            "Could not prepare thumbnail media; the video remains uploaded"
        ) from None

    failures = 0
    while True:
        try:
            response = youtube.thumbnails().set(videoId=video_id, media_body=media).execute(
                num_retries=0
            )
            if not isinstance(response, dict) or "items" not in response:
                # Some test clients expose a minimal acknowledgement. Real
                # thumbnails.set returns a thumbnailSetResponse with items.
                raise YouTubeThumbnailError(
                    "YouTube did not acknowledge the thumbnail; check the video before retrying"
                )
            return
        except YouTubeThumbnailError:
            raise
        except Exception as exc:
            retryable, status, reason, retry_after = _error_policy(exc)
            failures += 1
            if retryable and failures < max_failures:
                delay = min(64.0, 2 ** (failures - 1)) + jitter()
                sleep(max(delay, retry_after or 0.0))
                continue
            detail = "HTTP " + str(status) if status is not None else "network or client failure"
            if reason is not None:
                detail += "; reason=" + reason
            raise YouTubeThumbnailError(
                "Thumbnail operation failed (" + detail + "). Video upload is retained; "
                "retry only the thumbnail after checking YouTube Studio."
            ) from None
