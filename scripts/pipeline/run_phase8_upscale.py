from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ai_video_factory.domain import VideoClip
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.workflows.video_upscale import (
    build_video_upscale_plan,
    run_video_upscale,
)

REQUIRED_ENV = (
    "POSTGRES_DSN",
    "R2_ENDPOINT_URL",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def _environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upscale Phase 8 LTX clips 2x with self-hosted Real-ESRGAN on Salad."
    )
    parser.add_argument(
        "--clips",
        type=Path,
        default=Path("data/output/phase8/video_clips.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output/phase8"),
    )
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--dispatch-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--no-retry-terminal", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.clips.is_file():
        raise SystemExit(f"Required Phase 8 clips metadata not found: {args.clips}")
    raw = json.loads(args.clips.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"JSON file must contain an array: {args.clips}")
    clips = [VideoClip.model_validate(item) for item in raw]
    plan = build_video_upscale_plan(clips, clip_base_dir=args.clips.parent)

    environment = _environment()
    storage = create_r2_storage(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )
    queue = PostgresJobQueueClient(dsn=environment["POSTGRES_DSN"])
    manifest, upscaled = run_video_upscale(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=args.output_dir / "video_upscale_manifest.json",
        clips_dir=args.output_dir / "upscaled_clips",
        retry_terminal=not args.no_retry_terminal,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        dispatch_timeout_seconds=args.dispatch_timeout_seconds,
    )
    metadata_path = args.output_dir / "upscaled_clips.json"
    metadata_path.write_text(
        json.dumps([clip.model_dump(mode="json") for clip in upscaled], indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"run_fingerprint={manifest.run_fingerprint}")
    print(f"clips={metadata_path}")
    print(f"count={len(upscaled)}")
    print("Real-ESRGAN 1280x720 -> 2560x1440 clip upscale: OK")


if __name__ == "__main__":
    main()
