"""Publish an existing verified result without launching any generation stages.

Supported published layouts:
  data/<avatar>/<script>/                    (managed)
  data/results/<avatar>/<script>/<run_id>/    (immutable library)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from collections.abc import Sequence

from ai_video_factory.integrations.youtube.errors import YouTubeValidationError
from ai_video_factory.integrations.youtube.metadata import validate_video

try:
    from scripts.youtube.upload import main as upload_main
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.youtube"}:
        raise
    # Direct script execution starts with scripts/youtube on sys.path.
    from upload import main as upload_main


def _final_video(result: Path) -> Path:
    if result.is_symlink() or result.is_junction() or not result.is_dir():
        raise ValueError("A regular completed publication directory is required")
    manifest = result / "manifest.json"
    if manifest.is_symlink() or manifest.is_junction() or not manifest.is_file():
        raise ValueError("A verified final publication manifest is required")
    try:
        if manifest.stat().st_size > 1024 * 1024:
            raise ValueError("Invalid oversized publication manifest")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("schema_version") not in {
                "managed-b-pipeline-v1", "b-pipeline-publication-v1",
            }
            or not isinstance(data.get("files"), dict)
        ):
            raise ValueError("Invalid final publication manifest")
        candidates = [
            name for name in data["files"]
            if isinstance(name, str) and "/" not in name and "\\" not in name
            and name.lower().endswith(".mp4")
        ]
        if len(candidates) != 1:
            raise ValueError("A published result must contain exactly one final MP4")
        video = result / candidates[0]
        validate_video(video)  # Also checks published SHA-256 in this manifest.
        return video
    except (OSError, ValueError, UnicodeError, YouTubeValidationError):
        raise ValueError("No verified final MP4 in the chosen result directory") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish an existing verified Video Factory final result; no GPU/API generation"
    )
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--metadata", type=Path, help="Default: <result-dir>/youtube.json")
    parser.add_argument("--channel", help="Override authorized local channel alias")
    parser.add_argument("--thumbnail", type=Path, help="Override PNG/JPEG thumbnail")
    parser.add_argument("--state-dir", type=Path, help="Override shared publication registry")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--retry-failed-safe", action="store_true")
    args = parser.parse_args(argv)
    try:
        video = _final_video(args.result_dir)
    except ValueError as exc:
        parser.error(str(exc))
    metadata = args.metadata or (args.result_dir / "youtube.json")
    command = ["--video", str(video), "--metadata", str(metadata)]
    for name, value in (
        ("--channel", args.channel),
        ("--thumbnail", args.thumbnail),
        ("--state-dir", args.state_dir),
    ):
        if value is not None:
            command.extend([name, str(value)])
    if args.dry_run:
        command.append("--dry-run")
    if args.yes:
        command.append("--yes")
    if args.retry_failed_safe:
        command.append("--retry-failed-safe")
    return upload_main(command)


if __name__ == "__main__":
    raise SystemExit(main())
