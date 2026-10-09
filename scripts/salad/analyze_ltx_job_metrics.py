"""Offline, no-GPU diagnosis from normalized completed LTX job observations.

Input JSONL fields: job_id, instance_id, stage, profile, width, height,
num_frames, inference_seconds. Only same-profile/geometry/frame cohorts are
compared; this tool never drains or quarantines any worker.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_video_factory.inference.ltx_instance_scoring import classify_ltx_instances


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Normalized completed-jobs JSONL")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--min-jobs", type=int, default=3)
    parser.add_argument("--min-peer-instances", type=int, default=2)
    parser.add_argument("--slow-ratio", type=float, default=1.6)
    args = parser.parse_args()
    with args.input.open(encoding="utf-8") as source:
        records = [
            json.loads(line) for line in source if line.strip()
        ]
    scored = classify_ltx_instances(
        records,
        min_jobs=args.min_jobs,
        min_peer_instances=args.min_peer_instances,
        slow_ratio=args.slow_ratio,
    )
    if args.json_output:
        args.json_output.write_text(
            json.dumps({"instances": scored}, indent=2) + "\n",
            encoding="utf-8",
        )
    for row in scored:
        print(
            f"{row['stage']} {row['instance_id']} "
            f"{row['completed_jobs']} jobs {row['profile']} "
            f"{row['median_seconds_per_frame']:.4f}s/frame "
            f"ratio={row['ratio_to_peer_median']} "
            f"{row['classification']} ({row['confidence']})"
        )


if __name__ == "__main__":
    main()
