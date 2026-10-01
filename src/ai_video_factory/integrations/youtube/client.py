"""Build YouTube clients without loading Google dependencies in the main pipeline."""

from __future__ import annotations

from typing import Any

from .errors import YouTubeConfigurationError, YouTubeCredentialsError


def build_youtube_client(credentials: Any) -> Any:
    """Construct a YouTube Data API v3 client from already-authorized credentials.

    OAuth and refresh belong in auth.py (phase 2), not in this factory.
    """
    if credentials is None:
        raise YouTubeCredentialsError("YouTube credentials are required")
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise YouTubeConfigurationError(
            "Install the optional YouTube dependencies before using the YouTube client"
        ) from exc
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)
