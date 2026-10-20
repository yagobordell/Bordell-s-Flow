"""Post-verification YouTube hook; never influence audiovisual generation.

Only called AFTER existing local publication has verified the actual final MP4.
The external content registry is authoritative; per-run snapshots are informational.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_video_factory.config import Settings

from .errors import (
    YouTubeAmbiguousUploadError,
    YouTubeConfigurationError,
    YouTubeThumbnailError,
    YouTubeValidationError,
)
from .metadata import load_metadata, resolve_request
from .publisher import default_state_dir, publish_to_youtube
from .state import PublicationStateStore, _write_json, sha256_file
from .store import YouTubeStore


def _metadata_path(
    output: Path, published: Path, managed_target: Path | None
) -> Path:
    """Prefer the visible per-script metadata, not another run's .env defaults."""
    locations: list[Path] = []
    if managed_target is not None:
        locations.append(managed_target / "youtube.json")
    locations.append(output / "youtube.json")
    # An immutable results snapshot may contain metadata only if explicitly
    # added by a future pipeline; never look at a shared avatar/global JSON.
    for item in locations:
        if item.is_symlink() or item.is_junction():
            raise YouTubeValidationError("Linked youtube.json cannot be published")
        if item.is_file():
            return item
    raise YouTubeValidationError(
        "YOUTUBE_ENABLED is true but this finished run has no youtube.json; "
        "add explicit metadata before automatic publication"
    )


def publish_verified_result(
    *,
    output: Path,
    published: Path,
    script_name: str,
    config: Settings,
    managed_target: Path | None = None,
) -> dict[str, Any]:
    """Publish the locally verified immutable final result without regeneration."""
    if not config.youtube_enabled:
        return {"status": "disabled"}
    if config.youtube_keystore_dir is None:
        raise YouTubeConfigurationError(
            "Set YOUTUBE_KEYSTORE_DIR to the external authorized keystore"
        )
    if (
        published.is_symlink() or published.is_junction() or not published.is_dir()
        or Path(script_name).name != script_name
    ):
        raise YouTubeValidationError("A verified final result directory is required")
    manifest = published / "manifest.json"
    if manifest.is_symlink() or manifest.is_junction() or not manifest.is_file():
        raise YouTubeValidationError("Final publication manifest is missing or linked")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeError):
        raise YouTubeValidationError("Invalid final publication manifest") from None
    if (
        not isinstance(data, dict) or data.get("schema_version") not in {
            "managed-b-pipeline-v1", "b-pipeline-publication-v1",
        } or not isinstance(data.get("files"), dict)
    ):
        raise YouTubeValidationError("Unsupported final publication manifest")
    filename = script_name + ".mp4"
    sha = data["files"].get(filename)
    if not isinstance(sha, str) or len(sha) != 64:
        raise YouTubeValidationError("Final publication has no verified master MP4")
    video = published / filename
    if sha256_file(video) != sha:
        raise YouTubeValidationError("Master MP4 differs from the publication manifest")

    metadata = _metadata_path(output, published, managed_target)
    # The original Title PNG is the definitive thumbnail, not another
    # independently selected thumbnail from youtube.json.
    overrides = None
    title_report_path = output / "run_report.json"
    if title_report_path.is_file() and not title_report_path.is_symlink():
        run = json.loads(title_report_path.read_text(encoding="utf-8")).get("run", {})
        if isinstance(run, dict) and run.get("flow") == "titulo":
            selected = published / "thumbnail.png"
            if (selected.is_symlink() or not selected.is_file()
                    or sha256_file(selected) != run.get("thumbnail_sha256")):
                raise YouTubeValidationError("Title thumbnail differs from original PNG")
            declared = load_metadata(metadata).get("thumbnail_path")
            if declared is not None:
                candidate = Path(declared)
                if not candidate.is_absolute():
                    candidate = metadata.parent / candidate
                if (candidate.is_symlink() or not candidate.is_file()
                        or sha256_file(candidate) != run["thumbnail_sha256"]):
                    raise YouTubeValidationError(
                        "youtube.json thumbnail conflicts with Title input PNG"
                    )
            overrides = {"thumbnail_path": str(selected.absolute())}
    request = resolve_request(
        video_path=video,
        metadata_path=metadata,
        config=config,
        overrides=overrides,
    )
    # The uploader's local alias determines credentials, but it cannot verify
    # the actual channel without increasing the OAuth scope. Operators must
    # authorize and privately test each alias first.
    keystore = YouTubeStore(config.youtube_keystore_dir)
    keystore.ensure_directory()
    state_root = default_state_dir(config.youtube_keystore_dir, config.youtube_state_dir)
    snapshot = output / "youtube_publish.json"
    result = None
    try:
        result = publish_to_youtube(
            request,
            keystore_dir=keystore.directory,
            state_dir=state_root,
            state_path=snapshot,
        )
    finally:
        # Managed result folders remain intact: this state is intentionally
        # separate from the immutable media manifest and generation fingerprint.
        if managed_target is not None and snapshot.is_file():
            canonical = PublicationStateStore(state_root)
            digest = sha256_file(video)
            state = canonical.read(request.channel, digest)
            if state is not None:
                canonical.snapshot(state, managed_target / "youtube_publish.json")
    if result is None:
        raise YouTubeConfigurationError("YouTube publication has no confirmed result")
    return {
        "status": "completed",
        "video_id": result.video_id,
        "url": result.url,
        "privacy": result.privacy,
        "thumbnail_uploaded": result.thumbnail_uploaded,
    }


def record_distribution_error(output: Path, error: Exception) -> None:
    """Persist safe trigger failures separately from authoritative upload state.

    Preflight errors have no metadata fingerprint or video creation, so they
    cannot safely be represented as a content-indexed upload attempt.
    """
    if isinstance(error, YouTubeAmbiguousUploadError):
        status = "ambiguous"
    elif isinstance(error, YouTubeThumbnailError):
        status = "failed_after_upload"
    elif isinstance(error, (YouTubeValidationError, YouTubeConfigurationError)):
        status = "failed_safe"
    else:
        status = "failed_unknown"
    _write_json(output / "youtube_publish_error.json", {
        "schema_version": "youtube-publish-trigger/v1",
        "status": status,
        "error_type": type(error).__name__ if status != "failed_unknown" else "UnknownError",
        "occurred_at": datetime.now(UTC).isoformat(),
        "details": "Review youtube_publish.json and the external registry before a retry.",
    })
