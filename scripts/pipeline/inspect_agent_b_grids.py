"""Inspect Agent B logical sequences and physical image jobs without image API calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ai_video_factory.bots.agent_b.validation import sequence_index
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.openai_batch_images import described_beats


def inspect_grid_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("schema_version") != "b-pipeline-v1" or plan.get("planning_origin") != "agent_b":
        raise ValueError("Only the accepted single Agent B b-pipeline-v1 plan is supported.")
    assets = described_beats(plan)
    jobs = plan_image_jobs(plan, assets)
    return {
        "status": "planned_offline",
        "api_calls": 0,
        "described_assets": len(assets),
        "physical_jobs": len(jobs),
        "grids": sum(job.get("grid") is not None for job in jobs),
        "logical_sequences": sequence_index(plan),
        "jobs": [{
            "job_index": i,
            "block_id": job["block_id"],
            "beat_id": job["beat_id"],
            "asset_id": job.get("asset_id", job["beat_id"]),
            "kind": "grid" if job.get("grid") else "image",
            "sequence": job.get("logical_sequence_id"),
            "sequence_index": job.get("continuity_index"),
            "previous_block_id": job.get("continuity_previous_block_id"),
            "previous_asset_id": job.get("continuity_previous_asset_id"),
            "physical_panel_count": len([
                p for p in job.get("panels", []) if p.get("source") != "black_panel"
            ]),
            "panel_asset_ids": [
                p.get("asset_id", p["beat_id"])
                for p in job.get("panels", []) if p.get("source") != "black_panel"
            ],
            "size": job.get("size"),
        } for i, job in enumerate(jobs)],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    report = inspect_grid_plan(plan)
    formatted = json.dumps(report, ensure_ascii=False, indent=2)
    print(formatted)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.is_symlink():
            raise ValueError("Refusing symlink output report.")
        args.output.write_text(formatted + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
