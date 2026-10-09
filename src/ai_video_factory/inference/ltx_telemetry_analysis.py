"""Advisory comparisons of LTX worker inference on genuinely matched frame counts.

Pools, generation profiles, padded geometry and frame counts are never mixed.
The result can suggest review but never drives a drain or quarantine.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import median
from typing import Any


def compare_ltx_telemetry(
    rows: list[dict[str, Any]],
    *,
    min_jobs: int = 3,
    slow_ratio: float = 1.6,
) -> list[dict[str, Any]]:
    if min_jobs < 2 or not math.isfinite(slow_ratio) or slow_ratio <= 1:
        raise ValueError("Invalid telemetry comparison thresholds")
    groups: dict[tuple[str, str, int, int], dict[str, dict[int, list[float]]]] = (
        defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    )
    seen: set[tuple[str, int]] = set()
    for row in rows:
        seconds = row.get("inference_seconds")
        if seconds is None or not row.get("instance_id"):
            continue
        instance = str(row["instance_id"])
        frames = int(row["num_frames"])
        seconds = float(seconds)
        if frames <= 0 or not math.isfinite(seconds) or seconds <= 0:
            continue
        key = (str(row["job_id"]), int(row.get("attempt_count", 1)))
        if key in seen:
            raise ValueError("Duplicate LTX telemetry job attempt")
        seen.add(key)
        cohort = (
            str(row["stage"]), str(row["profile"]),
            int(row["width"]), int(row["height"]),
        )
        if min(cohort[2:]) <= 0:
            continue
        groups[cohort][instance][frames].append(seconds / frames)

    output: list[dict[str, Any]] = []
    for cohort, machines in sorted(groups.items()):
        for instance, by_frames in sorted(machines.items()):
            all_timings = [
                value for values in by_frames.values() for value in values
            ]
            paired_ratios: list[float] = []
            matched_jobs = 0
            compared_peers: set[str] = set()
            for frames, current in by_frames.items():
                other = [
                    sample
                    for peer, timings in machines.items() if peer != instance
                    for sample in timings.get(frames, [])
                ]
                if not other:
                    continue
                matched_jobs += len(current)
                compared_peers.update(
                    peer for peer, timings in machines.items()
                    if peer != instance and timings.get(frames)
                )
                paired_ratios.append(median(current) / median(other))
            sufficient = (
                len(all_timings) >= min_jobs
                and matched_jobs >= min_jobs
                and len(compared_peers) >= 1
            )
            ratio = median(paired_ratios) if sufficient else None
            label = (
                "review_candidate" if ratio is not None and ratio >= slow_ratio
                else "comparable" if ratio is not None
                else "insufficient_exact_frame_matches"
            )
            output.append({
                "stage": cohort[0],
                "profile": cohort[1],
                "width": cohort[2],
                "height": cohort[3],
                "instance_id": instance,
                "completed_jobs": len(all_timings),
                "frame_counts": sorted(by_frames),
                "exact_frame_matched_jobs": matched_jobs,
                "peer_instances": len(compared_peers),
                "median_inference_seconds_per_frame": round(median(all_timings), 5),
                "median_matched_ratio": round(ratio, 3) if ratio is not None else None,
                "classification": label,
            })
    return output
