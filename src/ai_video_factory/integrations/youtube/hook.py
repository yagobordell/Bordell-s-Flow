"""Post-verification YouTube hook; never influence audiovisual generation.

Only called AFTER existing local publication has verified the actual final MP4.
The external content registry is authoritative; per-run snapshots are informational.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_video_factory.config import Settings

from .errors import YouTubeConfigurationError, YouTubeValidationError
from .metadata import resolve_request
from .publisher import default_state_dir, publish_to_youtube
from .state import PublicationStateStore, sha256_file
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
    request = resolve_request(
        video_path=video,
        metadata_path=metadata,
        config=config,
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
