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
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
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


def inspect_manifest_state(
    plan,
    manifest_path: Path,
    *,
    archive_mismatch: bool = False,
) -> dict[str, object]:
    expected_fingerprint = video_generation_run_fingerprint(plan)
    state: dict[str, object] = {
        "status": "missing",
        "expected_fingerprint": expected_fingerprint,
        "existing_fingerprint": None,
        "active_resume_jobs": 0,
        "submitted_jobs": 0,
        "archived_path": None,
    }

    if not manifest_path.is_file():
        return state

    try:
        manifest = VideoGenerationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Existing Phase 8 video generation manifest is unreadable: "
            f"{manifest_path}: {exc}"
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
        _validate_manifest_against_plan(
            manifest,
            plan,
            expected_fingerprint,
        )
        state["status"] = "matching"
        return state

    state["status"] = "different_plan"
    if archive_mismatch:
        archived = _archive_path(manifest_path, manifest.run_fingerprint)
        os.replace(manifest_path, archived)
        state["archived_path"] = str(archived)
        state["status"] = "archived_different_plan"
    return state


def main() -> None:
    args = parse_args()
    if (args.width, args.height, args.fps) != (1280, 720, 24):
        raise SystemExit(
            "Phase 8 manifest inspection contract is exactly 1280x720 at 24 fps"
        )
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
    try:
        state = inspect_manifest_state(
            plan,
            args.manifest,
            archive_mismatch=args.archive_mismatch,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    expected_fingerprint = str(state["expected_fingerprint"])

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
