from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from r2_client import create_r2_storage

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.workflows.video_generation import build_video_generation_plan

REQUIRED_ENV = (
    "R2_ENDPOINT_URL",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit deterministic Phase 8 LTX outputs in R2 without touching Salad."
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
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def _environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _read_models(path: Path, model_type: type) -> list:
    if not path.is_file():
        raise SystemExit(f"Required JSON file not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise SystemExit(f"JSON file must contain an array: {path}")
    return [model_type.model_validate(item) for item in value]


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
    storage = create_r2_storage(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )

    records: list[dict[str, object]] = []
    for item in plan:
        status = "miss"
        error: str | None = None
        try:
            cached = cached_inference_response(storage, item.request)
        except RuntimeError as exc:
            cached = None
            status = "invalid"
            error = str(exc)
        if cached is not None:
            status = "hit"
        records.append(
            {
                "shot_id": item.shot_id,
                "status": status,
                "application_job_id": item.request.job_id,
                "request_sha256": item.request.fingerprint(),
                "object_key": item.request.output.key,
                "error": error,
            }
        )

    hits = sum(record["status"] == "hit" for record in records)
    misses = sum(record["status"] == "miss" for record in records)
    invalid = sum(record["status"] == "invalid" for record in records)
    print(
        f"Phase 8 cache: hits={hits} misses={misses} invalid={invalid} "
        f"total={len(records)} Salad submissions=0"
    )
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Audit JSON: {args.json_output.resolve()}")
    if invalid:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
