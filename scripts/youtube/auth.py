"""Local-only OAuth management for YouTube (never run on GPU/Salad workers).

Windows:
  uv run --locked --extra youtube python scripts/youtube/auth.py --channel "Canal Principal"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from ai_video_factory.config import settings
from ai_video_factory.integrations.youtube.auth import authorize_channel, load_credentials
from ai_video_factory.integrations.youtube.errors import YouTubeError
from ai_video_factory.integrations.youtube.store import YouTubeStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorize or check locally stored YouTube channel credentials."
    )
    parser.add_argument("--channel", help="Local channel alias; NOT verified by YouTube API")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true", help="List stored local channel aliases")
    actions.add_argument(
        "--check", action="store_true", help="Check/refresh credentials (no channel API query)"
    )
    actions.add_argument(
        "--reauthorize", action="store_true", help="Explicitly replace existing authorization"
    )
    parser.add_argument(
        "--yes", action="store_true", help="Confirm the local channel alias without a prompt"
    )
    args = parser.parse_args(argv)
    if args.list and args.channel:
        parser.error("--list does not take --channel")
    if not args.list and not args.channel:
        parser.error("--channel is required unless --list is selected")
    if settings.youtube_keystore_dir is None:
        parser.error("Set YOUTUBE_KEYSTORE_DIR as a Windows user environment variable")

    # The real script's location determines the checkout even with editable installs.
    checkout = Path(__file__).resolve().parents[2]
    try:
        store = YouTubeStore(settings.youtube_keystore_dir, repo_root=checkout)
        store.ensure_directory()
        if args.list:
            channels = store.list_channels()
            if not channels:
                print("No local YouTube channel credentials found.")
            else:
                for channel in channels:
                    print(channel)
            return 0
        if args.check:
            load_credentials(args.channel, store)
            print(
                "Local credentials are ready for " + args.channel +
                ". This does not verify the channel identity or test uploading."
            )
            return 0

        # The minimal youtube.upload scope does not grant channel identity read.
        print("Local alias: " + args.channel)
        print("Choose the intended YouTube channel carefully in the Google browser.")
        print("Actual destination will be confirmed by a PRIVATE test upload in phase 3.")
        if not args.yes:
            if not sys.stdin.isatty():
                parser.error("Interactive authorization requires a terminal (or --yes)")
            try:
                confirmation = input("Type AUTORIZAR to continue: ").strip()
            except EOFError:
                print("Authorization cancelled.")
                return 2
            if confirmation != "AUTORIZAR":
                print("Authorization cancelled.")
                return 2
        authorize_channel(args.channel, store, reauthorize=args.reauthorize)
        print(
            "Local credentials saved for " + args.channel +
            ". Channel identity is NOT verified. Perform a private test upload first."
        )
        return 0
    except YouTubeError as exc:
        # Errors from our OAuth boundary deliberately never include provider bodies/tokens.
        print("YouTube: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
