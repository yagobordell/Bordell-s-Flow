"""Single-session resumable YouTube video upload with secret-safe retry policy.

This module intentionally has no cross-run persistence. The phase-3 CLI
reserves an on-disk attempt before the first network chunk. Phase 5 will
replace its provisional receipt with full durable publisher state.
"""

from __future__ import annotations

import json
import random
import socket
import ssl
import time
from collections.abc import Callable
from typing import Any

from .errors import (
    YouTubeAmbiguousUploadError,
    YouTubeAuthenticationError,
    YouTubeConfigurationError,
    YouTubeUploadError,
)
from .metadata import build_insert_payload
from .models import YouTubePublishRequest, YouTubePublishResult

CHUNK_SIZE = 8 * 1024 * 1024
MAX_CONSECUTIVE_FAILURES = 5
TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
TRANSIENT_REASONS = frozenset({
    "backendError", "internalError", "rateLimitExceeded", "userRateLimitExceeded",
})
PERMANENT_REASONS = frozenset({
    "dailyLimitExceeded", "quotaExceeded", "uploadLimitExceeded",
})
KNOWN_REASONS = TRANSIENT_REASONS | PERMANENT_REASONS


def _safe_http_error(exc: Exception) -> tuple[int | None, str | None, float | None]:
    """Read only allowlisted fields, never raw Google HTTP errors or bodies."""
    response = getattr(exc, "resp", None)
    if response is None:
        return None, None, None
    try:
        status = int(response.status)
    except (AttributeError, TypeError, ValueError):
        return None, None, None
    reason = None
    try:
        content = getattr(exc, "content", b"")
        if isinstance(content, bytes):
            if len(content) > 16384:
                content = b""
            data = json.loads(content)
        elif isinstance(content, str) and len(content) <= 16384:
            data = json.loads(content)
        else:
            data = {}
        details = data.get("error", {})
        errors = details.get("errors", []) if isinstance(details, dict) else []
        if errors and isinstance(errors[0], dict):
            candidate = errors[0].get("reason")
            if candidate in KNOWN_REASONS:
                reason = candidate
    except (ValueError, TypeError, AttributeError, IndexError):
        pass
    retry_after = None
    try:
        header = response.get("retry-after")
        if header is not None:
            value = float(header)
            if 0 <= value <= 3600:
                retry_after = value
    except (AttributeError, TypeError, ValueError, OverflowError):
        pass
    return status, reason, retry_after


def _error_policy(exc: Exception) -> tuple[bool, int | None, str | None, float | None]:
    status, reason, retry_after = _safe_http_error(exc)
    if status is not None:
        if reason in PERMANENT_REASONS:
            return False, status, reason, retry_after
        return (
            status in TRANSIENT_HTTP or reason in TRANSIENT_REASONS,
            status,
            reason,
            retry_after,
        )
    is_network = isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError))
    is_network = is_network or isinstance(exc, OSError)
    # googleapiclient uses httplib2, whose errors need not inherit OSError.
    is_network = is_network or type(exc).__module__.startswith("httplib2")
    return is_network, None, None, None


def upload_video(
    youtube: Any,
    request: YouTubePublishRequest,
    *,
    media_factory: Callable[..., Any] | None = None,
    progress: Callable[[int], None] | None = None,
    on_started: Callable[[], None] | None = None,
    on_completed: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    max_failures: int = MAX_CONSECUTIVE_FAILURES,
) -> YouTubePublishResult:
    """Upload once; only retry within its single resumable request object.

    Callbacks are optional for library users. The CLI MUST supply on_started
    and on_completed to reserve the local attempt before its first chunk.
    """
    if max_failures < 1:
        raise ValueError("max_failures must be positive")
    if media_factory is None:
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError:
            raise YouTubeConfigurationError(
                "Install optional YouTube dependencies: uv sync --locked --extra youtube"
            ) from None
        media_factory = MediaFileUpload
    part, body = build_insert_payload(request)
    try:
        media = media_factory(
            filename=str(request.video_path),
            mimetype="video/mp4",
            chunksize=CHUNK_SIZE,
            resumable=True,
        )
        operation = youtube.videos().insert(
            part=part,
            body=body,
            media_body=media,
            notifySubscribers=request.notify_subscribers,
        )
    except Exception:
        raise YouTubeUploadError(
            "Could not prepare the YouTube upload; the video has not been sent"
        ) from None

    if on_started is not None:
        on_started()  # Reserve before ANY request to videos.insert leaves the machine.

    consecutive_failures = 0
    stalled_chunks = 0
    last_progress = -1
    while True:
        try:
            status, response = operation.next_chunk(num_retries=0)
        except Exception as exc:
            retryable, http, reason, retry_after = _error_policy(exc)
            consecutive_failures += 1
            if retryable and consecutive_failures < max_failures:
                delay = min(64.0, float(2 ** (consecutive_failures - 1))) + jitter()
                if retry_after is not None:
                    delay = max(delay, retry_after)
                sleep(delay)
                continue
            if http == 401 or (http == 403 and reason not in TRANSIENT_REASONS):
                raise YouTubeAuthenticationError(
                    "YouTube refused this request; verify authorization and upload permissions. "
                    "The result could be ambiguous: inspect the channel before retrying."
                ) from None
            if http is not None and not retryable:
                raise YouTubeUploadError(
                    "YouTube rejected the upload (HTTP " + str(http) + "). "
                    "Check metadata/quota; the attempt is locked against automatic retry."
                ) from None
            raise YouTubeAmbiguousUploadError(
                "Resumable upload interrupted or exhausted retries. It may have completed "
                "on YouTube: inspect the channel before starting another upload."
            ) from None

        if response is not None:
            video_id = response.get("id") if isinstance(response, dict) else None
            if (
                not isinstance(video_id, str) or not video_id
                or not all(char.isascii() and (char.isalnum() or char in "_-")
                           for char in video_id)
            ):
                raise YouTubeAmbiguousUploadError(
                    "YouTube returned no usable video ID; inspect the channel before retrying"
                )
            if on_completed is not None:
                on_completed(video_id)  # Immediately persist before any other operation.
            if progress is not None:
                progress(100)
            return YouTubePublishResult(
                video_id=video_id,
                url="https://www.youtube.com/watch?v=" + video_id,
                privacy=request.privacy,
                channel=request.channel,
                thumbnail_uploaded=False,
            )

        percentage = last_progress
        if status is not None:
            try:
                percentage = min(99, max(0, int(float(status.progress()) * 100)))
            except (TypeError, ValueError, AttributeError, OverflowError):
                percentage = last_progress
        if percentage > last_progress:
            last_progress = percentage
            consecutive_failures = 0
            stalled_chunks = 0
            if progress is not None:
                progress(percentage)
        else:
            stalled_chunks += 1
            if stalled_chunks >= 20:
                raise YouTubeAmbiguousUploadError(
                    "YouTube upload stalled without progress; inspect the channel before retrying"
                )
