"""Explicit human reconciliation for an interrupted/ambiguous publisher.

NO API requests, NO OAuth refresh, NO state clearing without operator input.
Use only after checking the actual authorized channel in YouTube Studio.
"""

from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from ai_video_factory.config import settings
from ai_video_factory.integrations.youtube.errors import YouTubeError
from ai_video_factory.integrations.youtube.metadata import validate_video
from ai_video_factory.integrations.youtube.publisher import default_state_dir
from ai_video_factory.integrations.youtube.state import PublicationStateStore, sha256_file
from ai_video_factory.integrations.youtube.store import validate_channel_name

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manually reconcile an ambiguous YouTube video after checking Studio."
    )
    parser.add_argument("--video", type=Path, required=True, help="Exact original MP4")
    parser.add_argument("--channel", required=True, help="Original local OAuth alias")
    parser.add_argument("--state-dir", type=Path, help="Shared registry directory")
    decision = parser.add_mutually_exclusive_group(required=True)
    decision.add_argument(
        "--found-video-id",
        help="Existing video ID manually confirmed in YouTube Studio",
    )
    decision.add_argument(
        "--verified-not-created", action="store_true",
        help="Only after exhaustively verifying that YouTube did not create the video",
    )
    parser.add_argument(
        "--acknowledge-studio-check", action="store_true",
        help="Explicitly confirm that the actual channel was inspected in Studio",
    )
    parser.add_argument("--yes", action="store_true", help="Skip interactive phrase")
    args = parser.parse_args(argv)
    if not args.acknowledge_studio_check:
        parser.error(
            "Inspect the actual channel in YouTube Studio and explicitly supply "
            "--acknowledge-studio-check"
        )
    if args.found_video_id is not None and _VIDEO_ID.fullmatch(args.found_video_id) is None:
        parser.error("Invalid YouTube video ID")
    if settings.youtube_keystore_dir is None and args.state_dir is None:
        parser.error("YOUTUBE_KEYSTORE_DIR or an absolute --state-dir is required")
    try:
        video, _ = validate_video(args.video)
        channel = validate_channel_name(args.channel)
        root = default_state_dir(
            settings.youtube_keystore_dir or args.state_dir,
            args.state_dir or settings.youtube_state_dir,
        )
        registry = PublicationStateStore(root)
        video_sha = sha256_file(video)
        with registry.locked(channel, video_sha):
            state = registry.read(channel, video_sha)
            if state is None:
                parser.error(
                    "No durable registry entry for this MP4 and alias; do not "
                    "use this tool to bypass a phase-3 local receipt"
                )
            if state["status"] not in ("uploading", "ambiguous"):
                parser.error(
                    "Reconciliation applies only to ambiguous/uploading attempts; "
                    "already-published videos cannot be overwritten here"
                )
            outcome = (
                "Existing video " + args.found_video_id
                if args.found_video_id else "No video created (operator verified)"
            )
            print("Current status: " + state["status"])
            print("Operator-selected decision: " + outcome)
            if not args.yes:
                phrase = "I VERIFIED THIS CHANNEL IN YOUTUBE STUDIO"
                try:
                    answer = input("Type exactly '" + phrase + "': ").strip()
                except EOFError:
                    return 2
                if answer != phrase:
                    print("No state changes were made.")
                    return 2
            state["reconciled_at"] = datetime.now(UTC).isoformat()
            state["manual_reconciliation"] = (
                "existing_video" if args.found_video_id else "verified_not_created"
            )
            if args.found_video_id:
                state["video_id"] = args.found_video_id
                state["url"] = (
                    "https://www.youtube.com/watch?v=" + args.found_video_id
                )
                state["status"] = "uploaded"
            else:
                state["status"] = "failed_safe"
                state["last_http_status"] = None
                state["last_api_reason"] = None
            registry.write(state)
        print("Durable state recorded. Do not start another insert without publisher checks.")
        if args.verified_not_created:
            print("Re-run publication only with explicit --retry-failed-safe.")
        else:
            print("Re-run the original publisher; it will reuse the existing video ID.")
        return 0
    except YouTubeError as exc:
        print(type(exc).__name__ + ": " + str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
