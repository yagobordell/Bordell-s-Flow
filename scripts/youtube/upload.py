"""Manual, confirmable YouTube publisher; no generation jobs or implicit API calls."""

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
from ai_video_factory.integrations.youtube.publisher import (
    default_state_dir,
    publish_to_youtube,
)
from ai_video_factory.integrations.youtube.state import PublicationStateStore, sha256_file
from ai_video_factory.integrations.youtube.store import YouTubeStore
from ai_video_factory.integrations.youtube.thumbnail import set_thumbnail
from ai_video_factory.integrations.youtube.uploader import upload_video


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish one MP4 with optional thumbnail, safely and idempotently."
    )
    parser.add_argument("--video", required=True, type=Path, help="Final MP4, never modified")
    parser.add_argument("--metadata", required=True, type=Path, help="youtube-publish/v1 JSON")
    parser.add_argument("--channel", help="Local OAuth alias (not verified channel ID)")
    parser.add_argument("--title", help="Override metadata title")
    parser.add_argument("--description", help="Override metadata description")
    parser.add_argument("--keyword", action="append", help="Override tags; repeat for each tag")
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"))
    parser.add_argument("--category-id")
    parser.add_argument("--default-language")
    parser.add_argument("--license", choices=("youtube", "creativeCommon"))
    parser.add_argument("--publish-at", help="RFC3339 future date, timezone and private")
    parser.add_argument("--recording-date", help="RFC3339 date with timezone")
    parser.add_argument("--thumbnail", type=Path, help="Optional PNG/JPEG override")
    parser.add_argument(
        "--state-dir", type=Path,
        help="One shared publication registry; defaults beside the external keystore",
    )
    parser.add_argument(
        "--retry-failed-safe", action="store_true",
        help="Explicit recovery ONLY for independently recorded failed_safe attempts",
    )
    for name in (
        "notify-subscribers", "made-for-kids", "contains-synthetic-media",
        "embeddable", "public-stats-viewable",
    ):
        parser.add_argument(
            "--" + name, action=argparse.BooleanOptionalAction, default=None
        )
    parser.add_argument("--dry-run", action="store_true", help="Local-only, no remote calls")
    parser.add_argument(
        "--yes", action="store_true", help="Deliberately bypass interactive confirmation"
    )
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    fields = (
        "channel", "title", "description", "privacy", "category_id",
        "default_language", "license", "publish_at", "recording_date",
        "notify_subscribers", "made_for_kids", "contains_synthetic_media",
        "embeddable", "public_stats_viewable",
    )
    values = {
        field: getattr(args, field) for field in fields if getattr(args, field) is not None
    }
    if args.keyword is not None:
        values["keywords"] = args.keyword
    if args.thumbnail is not None:
        values["thumbnail_path"] = str(args.thumbnail)
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
        # Dry-run must not refresh tokens or construct a Google client.
        inspect_local_credentials(request.channel, store)
        state_dir = default_state_dir(
            settings.youtube_keystore_dir, args.state_dir or settings.youtube_state_dir,
        )
        print(json.dumps(safe_summary(request, size), ensure_ascii=False, indent=2))
        if args.dry_run:
            registry = PublicationStateStore(state_dir)
            existing = registry.read(request.channel, sha256_file(video))
            if existing is not None:
                print("Existing durable publication: " + existing["status"])
            attempt = UploadAttempt(request)
            if attempt.path.exists() or attempt.path.is_symlink():
                print("Existing phase-3 receipt: inspect before a new upload.")
            print("Dry run successful: no remote calls, upload, token refresh or writes.")
            return 0

        print(
            "\nWARNING: OAuth aliases do not prove channel identity. "
            "For first tests use privacy=private."
        )
        if not args.yes:
            if not sys.stdin.isatty():
                parser.error("A terminal confirmation is required (or explicit --yes)")
            try:
                confirmation = input("Type SUBIR to publish or resume: ").strip()
            except EOFError:
                print("Publication cancelled. No video was sent.")
                return 2
            if confirmation != "SUBIR":
                print("Publication cancelled. No video was sent.")
                return 2

        def make_client() -> Any:
            return build_youtube_client(load_credentials(request.channel, store))

        # Manual snapshots are per source MP4, not a shared youtube.json in
        # directories containing several unrelated videos.
        snapshot = video.with_name(video.name + ".youtube_publish.json")
        result = publish_to_youtube(
            request,
            keystore_dir=settings.youtube_keystore_dir,
            state_dir=state_dir,
            state_path=snapshot,
            client_factory=make_client,
            upload_fn=upload_video,
            thumbnail_fn=set_thumbnail,
            progress=_progress,
            retry_failed_safe=args.retry_failed_safe,
        )
        print("YouTube publication complete or safely reused. ID: " + result.video_id)
        print("URL: " + result.url)
        print("Thumbnail uploaded: " + ("yes" if result.thumbnail_uploaded else "no"))
        print("Durable state: " + snapshot.name)
        return 0
    except YouTubeError as exc:
        print("YouTube: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Publication interrupted. Inspect the durable registry and YouTube Studio "
            "before another videos.insert; NEVER discard ambiguous state.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
