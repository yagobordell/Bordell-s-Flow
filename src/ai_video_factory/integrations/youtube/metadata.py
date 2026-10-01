"""Pure YouTube metadata resolution and offline file/request validation.

CLI overrides > per-video JSON > application Settings > model defaults.
No Google imports, credentials refresh, upload or filesystem writes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ai_video_factory.config import Settings

from .errors import YouTubeValidationError
from .models import YouTubePublishRequest

METADATA_SCHEMA = "youtube-publish/v1"
MAX_METADATA_BYTES = 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024**3


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def load_metadata(path: Path) -> dict[str, Any]:
    """Read a strict, small JSON document; never echo its contents on error."""
    file = Path(path).absolute()
    if file.is_symlink() or file.is_junction() or not file.is_file():
        raise YouTubeValidationError("youtube.json must be a regular, existing file")
    try:
        if file.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError("Oversized metadata")
        data = json.loads(file.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
        if not isinstance(data, dict) or data.pop("schema_version", None) != METADATA_SCHEMA:
            raise ValueError("Unsupported metadata schema")
        if "video_path" in data:
            raise ValueError("The MP4 must be supplied via --video")
        return data
    except (OSError, UnicodeError, ValueError):
        raise YouTubeValidationError(
            "Invalid youtube.json: expected schema youtube-publish/v1 and supported metadata"
        ) from None


def validate_video(path: Path) -> tuple[Path, int]:
    """Verify a stable regular MP4 with an ISO-BMFF ftyp header (not a full media probe)."""
    file = Path(path).absolute()
    if file.is_symlink() or file.is_junction() or not file.is_file():
        raise YouTubeValidationError("MP4 must be an existing regular file, not a link")
    if file.suffix.casefold() != ".mp4":
        raise YouTubeValidationError("The selected video must be an MP4 file")
    try:
        stat = file.stat()
        if stat.st_size < 12 or stat.st_size > MAX_VIDEO_BYTES:
            raise YouTubeValidationError("MP4 size must be greater than zero and at most 256 GiB")
        with file.open("rb") as stream:
            header = stream.read(12)
        if header[4:8] != b"ftyp":
            raise YouTubeValidationError("MP4 is missing its expected ftyp container header")
    except OSError:
        raise YouTubeValidationError("Cannot access the selected MP4") from None
    return file, stat.st_size


def resolve_request(
    *,
    video_path: Path,
    metadata_path: Path,
    config: Settings,
    overrides: Mapping[str, Any] | None = None,
) -> YouTubePublishRequest:
    """Resolve fields before Pydantic defaults; unknown fields always fail closed."""
    defaults: dict[str, Any] = {
        "category_id": config.youtube_default_category_id,
        "privacy": config.youtube_default_privacy,
        "notify_subscribers": config.youtube_notify_subscribers,
        "made_for_kids": config.youtube_made_for_kids,
        "contains_synthetic_media": config.youtube_contains_synthetic_media,
        "embeddable": config.youtube_embeddable,
        "public_stats_viewable": config.youtube_public_stats_viewable,
        "license": config.youtube_license,
    }
    if config.youtube_channel is not None:
        defaults["channel"] = config.youtube_channel
    if config.youtube_default_language is not None:
        defaults["default_language"] = config.youtube_default_language
    source = load_metadata(metadata_path)
    merged = {**defaults, **source, **(overrides or {})}
    if merged.get("thumbnail_path") is not None:
        raise YouTubeValidationError(
            "Thumbnail upload belongs to phase 4; remove thumbnail_path for this phase"
        )
    merged.pop("thumbnail_path", None)
    merged["video_path"], _ = validate_video(video_path)
    if "title" not in merged or not isinstance(merged["title"], str):
        raise YouTubeValidationError("YouTube title is required in youtube.json or --title")
    if not isinstance(merged.get("channel"), str) or not merged["channel"]:
        raise YouTubeValidationError("YouTube channel is required in metadata, Settings or CLI")
    try:
        return YouTubePublishRequest.model_validate(merged)
    except ValidationError as exc:
        # ValidationError may echo user-supplied values or secrets in fields.
        fields = sorted({
            str(error["loc"][0]) for error in exc.errors(include_input=False)
            if error["loc"]
        })
        raise YouTubeValidationError(
            "Invalid YouTube metadata fields: " + ", ".join(fields or ["unknown"])
        ) from None


def build_insert_payload(request: YouTubePublishRequest) -> tuple[str, dict[str, Any]]:
    """Construct only API-writable parts without any Google network calls."""
    snippet: dict[str, Any] = {
        "title": request.title,
        "description": request.description,
        "categoryId": request.category_id,
    }
    if request.keywords:
        snippet["tags"] = list(request.keywords)
    if request.default_language:
        snippet["defaultLanguage"] = request.default_language
    status: dict[str, Any] = {
        "privacyStatus": request.privacy,
        "selfDeclaredMadeForKids": request.made_for_kids,
        "embeddable": request.embeddable,
        "license": request.license,
        "publicStatsViewable": request.public_stats_viewable,
    }
    if request.contains_synthetic_media is not None:
        status["containsSyntheticMedia"] = request.contains_synthetic_media
    if request.publish_at is not None:
        status["publishAt"] = request.publish_at.isoformat()
    body: dict[str, Any] = {"snippet": snippet, "status": status}
    parts = ["snippet", "status"]
    if request.localizations:
        body["localizations"] = {
            language: localization.model_dump()
            for language, localization in request.localizations.items()
        }
        parts.append("localizations")
    if request.recording_date is not None:
        body["recordingDetails"] = {"recordingDate": request.recording_date.isoformat()}
        parts.append("recordingDetails")
    return ",".join(parts), body


def safe_summary(request: YouTubePublishRequest, size: int) -> dict[str, Any]:
    """A dry-run summary without tokens, credentials or Google API calls."""
    parts, body = build_insert_payload(request)
    return {
        "channel_alias_unverified": request.channel,
        "video": request.video_path.name,
        "video_bytes": size,
        "title": request.title,
        "description_bytes": len(request.description.encode("utf-8")),
        "tags": list(request.keywords),
        "privacy": request.privacy,
        "publish_at": request.publish_at.isoformat() if request.publish_at else None,
        "parts": parts,
        "api_body": body,
        "notify_subscribers": request.notify_subscribers,
        "identity_verified": False,
    }
