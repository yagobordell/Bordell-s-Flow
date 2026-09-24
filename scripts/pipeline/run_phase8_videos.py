from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.workflows.video_generation import (
    VideoGenerationManifest,
    build_video_generation_plan,
    run_video_generation,
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


def _read_models[ModelT](path: Path, model_type: type[ModelT]) -> list[ModelT]:
    if not path.is_file():
        raise SystemExit(f"Required JSON file not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise SystemExit(f"JSON file must contain an array: {path}")
    return [model_type.model_validate(item) for item in value]  # type: ignore[attr-defined]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _manifest_progress_signature(
    manifest: VideoGenerationManifest,
) -> tuple[tuple[int, str, int, str | None], ...]:
    return tuple(
        (
            state.shot_id,
            state.transport_status,
            state.submission_count,
            state.transport_job_id,
        )
        for state in manifest.jobs
    )


def _watch_manifest(
    path: Path,
    stop: threading.Event,
    *,
    interval_seconds: float,
) -> None:
    previous: tuple[tuple[int, str, int, str | None], ...] | None = None

    while not stop.is_set():
        if path.is_file():
            try:
                manifest = VideoGenerationManifest.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                manifest = None

            if manifest is not None:
                signature = _manifest_progress_signature(manifest)
                if signature != previous:
                    print("progress:", flush=True)
                    for shot_id, status, submissions, transport_job_id in signature:
                        print(
                            f"  shot={shot_id} status={status} submissions={submissions} "
                            f"transport_job_id={transport_job_id}",
                            flush=True,
                        )
                    previous = signature

        stop.wait(interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fan out, resume and verify all LTX-2.5 video jobs through Postgres/R2 on Salad compute."
    )
    parser.add_argument(
        "--keyframes",
        type=Path,
        default=Path("data/output/phase6/storyboard_keyframes.json"),
    )
    parser.add_argument(
        "--prompts",
        type=Path,
        default=Path("data/output/phase8/video_prompts.json"),
    )
    parser.add_argument(
        "--timings",
        type=Path,
        default=Path("data/output/phase5/shot_timings.json"),
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seed-base", type=int, default=42)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output/phase8"),
    )
    parser.add_argument(
        "--submit-only",
        action="store_true",
        help="Submit unresolved jobs and persist the manifest without polling for completion.",
    )
    parser.add_argument(
        "--no-retry-terminal",
        action="store_true",
        help="Do not resubmit transports already recorded as failed or cancelled.",
    )
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--timeout-seconds", type=float, default=21600.0)
    parser.add_argument("--dispatch-timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--progress-seconds",
        type=float,
        default=5.0,
        help="Local manifest refresh interval while waiting; use 0 to disable progress output.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.progress_seconds < 0:
        raise SystemExit("--progress-seconds must be >= 0")
    if (args.width, args.height, args.fps) != (1280, 720, 24):
        raise SystemExit("Phase 8 production video contract is exactly 1280x720 at 24 fps")

    environment = _environment()

    keyframes = _read_models(args.keyframes, StoryboardKeyframe)
    prompts = _read_models(args.prompts, VideoPrompt)
    timings = _read_models(args.timings, ShotTiming)

    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=args.keyframes.parent,
        width=args.width,
        height=args.height,
        fps=args.fps,
        seed_base=args.seed_base,
    )

    storage = create_r2_storage(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )
    queue = PostgresJobQueueClient(dsn=environment["POSTGRES_DSN"])

    manifest_path = args.output_dir / "video_generation_manifest.json"
    clips_dir = args.output_dir / "video_clips"

    stop_progress = threading.Event()
    progress_thread: threading.Thread | None = None
    if not args.submit_only and args.progress_seconds > 0:
        progress_thread = threading.Thread(
            target=_watch_manifest,
            args=(manifest_path, stop_progress),
            kwargs={"interval_seconds": args.progress_seconds},
            daemon=True,
            name="phase8-manifest-progress",
        )
        progress_thread.start()

    try:
        manifest, clips = run_video_generation(
            plan,
            queue=queue,
            storage=storage,
            manifest_path=manifest_path,
            clips_dir=clips_dir,
            wait=not args.submit_only,
            retry_terminal=not args.no_retry_terminal,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
            dispatch_timeout_seconds=args.dispatch_timeout_seconds,
            transport_route="postgres:ltx25",
        )
    finally:
        if progress_thread is not None:
            stop_progress.set()
            progress_thread.join(timeout=max(1.0, args.progress_seconds + 1.0))

    print(f"run_fingerprint={manifest.run_fingerprint}")
    for state in manifest.jobs:
        print(
            f"shot={state.shot_id} application_job_id={state.application_job_id} "
            f"transport_job_id={state.transport_job_id} status={state.transport_status} "
            f"submissions={state.submission_count}"
        )

    if args.submit_only:
        print(f"manifest={manifest_path}")
        print("LTX-2.5 fanout submitted; rerun without --submit-only to resume and fan in.")
        return

    clips_path = args.output_dir / "video_clips.json"
    _write_json(clips_path, [clip.model_dump(mode="json") for clip in clips])
    print(f"clips={clips_path}")
    print(f"count={len(clips)}")
    print("LTX-2.5 video fanout/resume: OK")


if __name__ == "__main__":
    main()
