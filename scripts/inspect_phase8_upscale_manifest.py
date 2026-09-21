from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ai_video_factory.domain import VideoClip
from ai_video_factory.workflows.video_upscale import (
    VideoUpscaleManifest,
    build_video_upscale_plan,
    video_upscale_run_fingerprint,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect the Phase 8 upscale resume manifest.")
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--archive-mismatch", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw = json.loads(args.clips.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"JSON file must contain an array: {args.clips}")
    plan = build_video_upscale_plan(
        [VideoClip.model_validate(item) for item in raw],
        clip_base_dir=args.clips.parent,
    )
    fingerprint = video_upscale_run_fingerprint(plan)
    status = "missing"
    active = 0
    terminal_retry_jobs = 0
    archived_to: str | None = None
    if args.manifest.is_file():
        manifest = VideoUpscaleManifest.model_validate_json(
            args.manifest.read_text(encoding="utf-8")
        )
        if manifest.run_fingerprint == fingerprint:
            status = "matching"
            active = sum(
                state.transport_status in {"pending", "running"} for state in manifest.jobs
            )
            terminal_retry_jobs = sum(
                state.transport_status in {"failed", "cancelled"} for state in manifest.jobs
            )
        else:
            status = "different_plan"
            active = sum(
                state.transport_status in {"pending", "running"} for state in manifest.jobs
            )
            terminal_retry_jobs = sum(
                state.transport_status in {"failed", "cancelled"} for state in manifest.jobs
            )
            if args.archive_mismatch:
                if active:
                    raise SystemExit(
                        "Refusing to archive a different upscale plan with active transports"
                    )
                archive = args.manifest.with_name(
                    f"{args.manifest.stem}.{manifest.run_fingerprint[:12]}.stale.json"
                )
                os.replace(args.manifest, archive)
                archived_to = str(archive)
                status = "archived_different_plan"

    payload = {
        "status": status,
        "run_fingerprint": fingerprint,
        "active_resume_jobs": active,
        "terminal_retry_jobs": terminal_retry_jobs,
        "archived_to": archived_to,
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
