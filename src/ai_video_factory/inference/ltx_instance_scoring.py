"""Observation-only LTX per-machine scoring from comparable completed jobs.

Never quarantine or drain workers here. Real GPUs differ in workload mix,
video geometry, pipeline profile and bootstrap; do not compare unrelated jobs.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import median
from typing import Any


def classify_ltx_instances(
    jobs: list[dict[str, Any]],
    *,
    min_jobs: int = 3,
    min_peer_instances: int = 2,
    slow_ratio: float = 1.6,
) -> list[dict[str, Any]]:
    """Assess each (pool, profile, geometry, frame count, machine) cohort."""
    if min_jobs < 2 or min_peer_instances < 1 or slow_ratio <= 1:
        raise ValueError("Invalid classifier thresholds")
    cohorts: dict[tuple[str, str, int, int, int], dict[str, list[float]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    seen_ids: set[str] = set()
    for job in jobs:
        job_id = str(job["job_id"])
        instance = str(job["instance_id"])
        stage = str(job["stage"])
        profile = str(job["profile"])
        width = int(job["width"])
        height = int(job["height"])
        frames = int(job["num_frames"])
        seconds = float(job["inference_seconds"])
        if (
            not all((job_id, instance, stage, profile))
            or min(width, height, frames) <= 0
            or not math.isfinite(seconds) or seconds <= 0
        ):
            raise ValueError("Invalid completed LTX job observation")
        if job_id in seen_ids:
            raise ValueError("Duplicate LTX job observation")
        seen_ids.add(job_id)
        key = stage, profile, width, height, frames
        cohorts[key][instance].append(seconds / frames)
    results: list[dict[str, Any]] = []
    for key, instances in sorted(cohorts.items()):
        for instance, timings in sorted(instances.items()):
            peers = [
                value
                for peer_id, samples in instances.items()
                if peer_id != instance and len(samples) >= min_jobs
                for value in samples
            ]
            peer_instances = sum(
                peer_id != instance and len(samples) >= min_jobs
                for peer_id, samples in instances.items()
            )
            enough = len(timings) >= min_jobs and peer_instances >= min_peer_instances
            ratio = median(timings) / median(peers) if enough else None
            verdict = (
                "suspected_slow"
                if ratio is not None and ratio >= slow_ratio
                else "normal"
                if ratio is not None
                else "insufficient_comparable_data"
            )
            results.append({
                "stage": key[0],
                "profile": key[1],
                "width": key[2],
                "height": key[3],
                "num_frames": key[4],
                "instance_id": instance,
                "completed_jobs": len(timings),
                "peer_instances": peer_instances,
                "median_seconds_per_frame": round(median(timings), 5),
                "ratio_to_peer_median": round(ratio, 3) if ratio is not None else None,
                "confidence": (
                    "higher" if enough and len(timings) >= 8
                    else "preliminary" if enough
                    else "insufficient"
                ),
                "classification": verdict,
            })
    return results
