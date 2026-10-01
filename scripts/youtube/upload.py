"""Explicit, local-only phase-3 uploader; never used by Video Factory runners.

Windows:
  uv run --locked --extra youtube python scripts/youtube/upload.py `
    --channel test01 --video C:\\Videos\\sample.mp4 `
    --metadata C:\\Videos\\youtube.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.integrations.youtube.attempt import UploadAttempt
from ai_video_factory.integrations.youtube.auth import (
    inspect_local_credentials,
    load_credentials,
)
from ai_video_factory.integrations.youtube.client import build_youtube_client
from ai_video_factory.integrations.youtube.errors import YouTubeError
from ai_video_factory.integrations.youtube.metadata import (
    resolve_request,
    safe_summary,
    validate_video,
)
from ai_video_factory.integrations.youtube.store import YouTubeStore
from ai_video_factory.integrations.youtube.uploader import upload_video


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload one MP4 to an authorized YouTube channel (phase 3)."
    )
    parser.add_argument("--video", required=True, type=Path, help="Source MP4, never modified")
    parser.add_argument("--metadata", required=True, type=Path, help="youtube-publish/v1 JSON")
    parser.add_argument("--channel", help="Local OAuth alias (not verified channel ID)")
    parser.add_argument("--title", help="Override metadata title")
    parser.add_argument("--description", help="Override metadata description")
    parser.add_argument("--keyword", action="append", help="Override tags; repeat per tag")
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"))
    parser.add_argument("--category-id")
    parser.add_argument("--default-language")
    parser.add_argument("--license", choices=("youtube", "creativeCommon"))
    parser.add_argument("--publish-at", help="RFC3339 date with timezone, future and private")
    parser.add_argument("--recording-date", help="RFC3339 date with timezone")
    for name in (
        "notify-subscribers", "made-for-kids", "contains-synthetic-media",
        "embeddable", "public-stats-viewable",
    ):
        parser.add_argument(
            "--" + name, action=argparse.BooleanOptionalAction, default=None
        )
    parser.add_argument("--dry-run", action="store_true", help="Only local preflight; no network")
    parser.add_argument(
        "--yes", action="store_true", help="Explicitly skip the interactive confirmation"
    )
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    fields = (
        "channel", "title", "description", "privacy", "category_id",
        "default_language", "license", "publish_at", "recording_date",
        "notify_subscribers", "made_for_kids", "contains_synthetic_media",
        "embeddable", "public_stats_viewable",
    )
    values = {field: getattr(args, field) for field in fields
              if getattr(args, field) is not None}
    if args.keyword is not None:
        values["keywords"] = args.keyword
    return values


def _progress(percent: int) -> None:
    print(f"  YouTube upload: {percent}%", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if settings.youtube_keystore_dir is None:
        parser.error("YOUTUBE_KEYSTORE_DIR must point to your external credential folder")
    try:
        video, size = validate_video(args.video)
        request = resolve_request(
            video_path=video,
            metadata_path=args.metadata,
            config=settings,
            overrides=_overrides(args),
        )
        store = YouTubeStore(
            settings.youtube_keystore_dir, repo_root=Path(__file__).resolve().parents[2]
        )
        # Offline preflight: no refresh, Google API call or OAuth interactive flow.
        inspect_local_credentials(request.channel, store)
        print(json.dumps(safe_summary(request, size), ensure_ascii=False, indent=2))
        if args.dry_run:
            attempt = UploadAttempt(request)
            if attempt.path.exists() or attempt.path.is_symlink():
                print(
                    "NOTE: an upload attempt record already exists for this video path; "
                    "real upload may be blocked."
                )
            print("Dry run successful: no remote calls, upload or token refresh.")
            return 0

        print(
            "\nWARNING: OAuth aliases do not prove channel identity. "
            "For the first test use privacy=private."
        )
        if not args.yes:
            if not sys.stdin.isatty():
                parser.error("A terminal confirmation is required (or explicit --yes)")
            try:
                confirmation = input("Type SUBIR to begin the actual upload: ").strip()
            except EOFError:
                print("Upload cancelled. No video was sent.")
                return 2
            if confirmation != "SUBIR":
                print("Upload cancelled. No video was sent.")
                return 2

        attempt = UploadAttempt(request)
        attempt.fingerprint()
        previous = attempt.existing_success()
        if previous is not None:
            print(
                "Reusing previous completed upload without sending anything: "
                + previous.url
            )
            return 0
        credentials = load_credentials(request.channel, store)
        try:
            youtube = build_youtube_client(credentials)
        except Exception:
            print("YouTube client could not be initialized. No upload was sent.", file=sys.stderr)
            return 2
        result = upload_video(
            youtube,
            request,
            on_started=attempt.reserve,
            on_completed=attempt.complete,
            on_failure=attempt.record_failure,
            progress=_progress,
        )
        print("YouTube upload complete. ID: " + result.video_id)
        print("URL: " + result.url)
        print("Local receipt: " + attempt.path.name)
        print("Check the actual channel and the private video before further publishing.")
        return 0
    except YouTubeError as exc:
        # All integration exceptions crossing this boundary contain only safe summaries.
        print("YouTube: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Upload interrupted. If an attempt record exists, inspect the YouTube "
            "channel before retrying; NEVER discard the record blindly.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
