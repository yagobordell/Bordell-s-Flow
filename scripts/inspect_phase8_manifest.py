from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.workflows.video_generation import (
    VideoGenerationManifest,
    _validate_manifest_against_plan,
    build_video_generation_plan,
    video_generation_run_fingerprint,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect whether the persisted Phase 8 resume manifest belongs to the "
            "current deterministic generation plan."
        )
    )
    parser.add_argument("--keyframes", type=Path, required=True)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--timings", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seed-base", type=int, default=42)
    parser.add_argument(
        "--archive-mismatch",
        action="store_true",
        help="Archive a valid manifest only when its run fingerprint differs.",
    )
    return parser.parse_args()


def _read_models(path: Path, model_type: type) -> list:
    if not path.is_file():
        raise SystemExit(f"Required JSON file not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise SystemExit(f"JSON file must contain an array: {path}")
    return [model_type.model_validate(item) for item in value]


def _archive_path(manifest_path: Path, fingerprint: str) -> Path:
    archive_dir = manifest_path.parent / "manifest_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    safe_fingerprint = fingerprint[:16] if fingerprint else "unknown"
    return archive_dir / f"video_generation_manifest.{safe_fingerprint}.json"


def main() -> None:
    args = parse_args()
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
    expected_fingerprint = video_generation_run_fingerprint(plan)

    state: dict[str, object] = {
        "status": "missing",
        "expected_fingerprint": expected_fingerprint,
        "existing_fingerprint": None,
        "active_resume_jobs": 0,
        "submitted_jobs": 0,
        "archived_path": None,
    }

    if args.manifest.is_file():
        try:
            manifest = VideoGenerationManifest.model_validate_json(
                args.manifest.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"Existing Phase 8 video generation manifest is unreadable: "
                f"{args.manifest}: {exc}"
            ) from exc

        state["existing_fingerprint"] = manifest.run_fingerprint
        state["submitted_jobs"] = sum(
            job.transport_job_id is not None for job in manifest.jobs
        )
        state["active_resume_jobs"] = sum(
            job.transport_job_id is not None
            and job.transport_status in {"pending", "running"}
            for job in manifest.jobs
        )

        if manifest.run_fingerprint == expected_fingerprint:
            try:
                _validate_manifest_against_plan(
                    manifest,
                    plan,
                    expected_fingerprint,
                )
            except ValueError as exc:
                raise SystemExit(
                    f"Existing Phase 8 manifest matches the run fingerprint but is "
                    f"structurally inconsistent: {exc}"
                ) from exc
            state["status"] = "matching"
        else:
            state["status"] = "different_plan"
            if args.archive_mismatch:
                archived = _archive_path(args.manifest, manifest.run_fingerprint)
                os.replace(args.manifest, archived)
                state["archived_path"] = str(archived)
                state["status"] = "archived_different_plan"

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "PHASE8_MANIFEST_STATE "
        f"status={state['status']} "
        f"active_resume_jobs={state['active_resume_jobs']} "
        f"submitted_jobs={state['submitted_jobs']} "
        f"expected={expected_fingerprint}"
    )
    if state["archived_path"] is not None:
        print(f"PHASE8_MANIFEST_ARCHIVED path={state['archived_path']}")


if __name__ == "__main__":
    main()
