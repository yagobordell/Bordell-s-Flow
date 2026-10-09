"""Reproducible LTX cost/latency projections. Offline; NEVER starts GPUs.

Input: JSON with {"stages": [{"stage": "ltx25_i2v", "remaining_work_seconds":
12220, "active_jobs": 52, "running_jobs": 0, "current_replicas": 0,
"max_replicas": 4, "bootstrap_seconds": 1400, "target_seconds": 3600,
"utilization": 0.85, "hourly_usd": 0.50}]}.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ai_video_factory.inference.ltx_economics import plan_ltx_capacity


def evaluate_scenario(document: dict[str, Any]) -> dict[str, Any]:
    stages = document.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("Scenario must contain at least one LTX stage")
    selections: list[dict[str, Any]] = []
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("stage"), str):
            raise ValueError("Invalid stage scenario")
        parameters = {
            key: value for key, value in stage.items() if key != "stage"
        }
        decision = plan_ltx_capacity(**parameters)
        selections.append({
            "stage": stage["stage"],
            "selected": asdict(decision.selected),
            "reason": decision.reason,
            "candidates": [asdict(option) for option in decision.options],
        })
    return {
        "scope": "illustrative forward pool projection, not a per-video invoice",
        "assumptions": (
            "cold starts modeled; all selected replicas conservatively counted "
            "as billable from t=0; no availability/retry/interruption modeling"
        ),
        "stages": selections,
        "projected_parallel_finish_seconds": max(
            selection["selected"]["eta_seconds"] for selection in selections
        ),
        "projected_usd": round(sum(
            selection["selected"]["projected_usd"] for selection in selections
        ), 6),
        "projected_gpu_seconds": round(sum(
            selection["selected"]["projected_gpu_seconds"]
            for selection in selections
        ), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    document = json.loads(args.scenario.read_text(encoding="utf-8"))
    result = evaluate_scenario(document)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.json_output is not None:
        args.json_output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
