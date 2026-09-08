from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.gpu.storage import R2ObjectStorage
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workflows.video_generation import (
    build_video_generation_plan,
    run_video_generation,
)

REQUIRED_ENV = (
    "SALAD_API_KEY",
    "SALAD_ORGANIZATION",
    "SALAD_PROJECT",
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fan out, resume and verify all Phase 8 LTX video jobs through Salad/R2."
    )
    parser.add_argument(
        "--queue-name",
        default=os.getenv("SALAD_QUEUE_NAME", "ai-video-factory-jobs"),
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
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=1280)
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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

    storage = R2ObjectStorage.create(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )
    queue = SaladJobQueueClient(
        organization=environment["SALAD_ORGANIZATION"],
        project=environment["SALAD_PROJECT"],
        queue_name=args.queue_name,
        api_key=environment["SALAD_API_KEY"],
    )

    manifest_path = args.output_dir / "video_generation_manifest.json"
    clips_dir = args.output_dir / "video_clips"
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
    )

    print(f"run_fingerprint={manifest.run_fingerprint}")
    for state in manifest.jobs:
        print(
            f"shot={state.shot_id} application_job_id={state.application_job_id} "
            f"transport_job_id={state.transport_job_id} status={state.transport_status} "
            f"submissions={state.submission_count}"
        )

    if args.submit_only:
        print(f"manifest={manifest_path}")
        print("Phase 8.4 fanout submitted; rerun without --submit-only to resume and fan in.")
        return

    clips_path = args.output_dir / "video_clips.json"
    _write_json(clips_path, [clip.model_dump(mode="json") for clip in clips])
    print(f"clips={clips_path}")
    print(f"count={len(clips)}")
    print("Phase 8.4 video fanout/resume: OK")


if __name__ == "__main__":
    main()
