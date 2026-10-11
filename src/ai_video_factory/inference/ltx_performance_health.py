"""Conservative, deterministic LTX instance-health assessments from completed jobs.

This module has no provider access and cannot drain or terminate a GPU. Only
verified Salad instance identities and exactly matched warm-job cohorts count.
A provisional progress signal must never be passed to this classifier.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any


def assess_ltx_instance_health(
    observations: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    max_age_days: int = 7,
    min_matches: int = 5,
    min_peer_samples: int = 3,
    suspected_ratio: float = 2.0,
    confirmed_ratio: float = 3.0,
) -> list[dict[str, Any]]:
    """Classify Salad instances; exact profile, stage, shape and frames only.

    Do not interpret this as proof of a bad physical GPU. The recorded identity
    is a Salad *instance*, not necessarily a stable physical host identifier.
    """
    if (
        min_matches < 2 or min_peer_samples < 2 or max_age_days < 1
        or not (1 < suspected_ratio < confirmed_ratio)
    ):
        raise ValueError("Invalid LTX instance health thresholds")
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        raise ValueError("now must have timezone information")
    cutoff = observed_at - timedelta(days=max_age_days)
    # Preserve one completed attempt per job; duplicate/inconsistent rows
    # are excluded completely rather than counted as independent evidence.
    by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        if row.get("job_id"):
            by_job[str(row["job_id"])].append(row)

    cohorts: dict[
        tuple[str, str, int, int, int], dict[str, list[float]]
    ] = defaultdict(lambda: defaultdict(list))
    for copies in by_job.values():
        if len(copies) != 1:
            continue
        row = copies[0]
        if (
            row.get("instance_identity_source") != "salad_instance"
            or row.get("pipeline_reused") is not True
            or row.get("attempt_count") != 1
        ):
            continue
        recorded_at = row.get("recorded_at")
        if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None:
            continue
        if not cutoff <= recorded_at <= observed_at:
            continue
        try:
            stage = str(row["stage"])
            profile = str(row["profile"])
            instance_id = str(row["instance_id"])
            width = int(row["width"])
            height = int(row["height"])
            frames = int(row["num_frames"])
            seconds = float(row["inference_seconds"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if (
            not stage or not profile or not instance_id
            or min(width, height, frames) < 1
            or not math.isfinite(seconds) or seconds <= 0
        ):
            continue
        cohorts[(stage, profile, width, height, frames)][instance_id].append(
            seconds / frames
        )

    by_instance: dict[tuple[str, str], dict[str, Any]] = {}
    for cohort, instances in cohorts.items():
        stage = cohort[0]
        for instance, timings in instances.items():
            result = by_instance.setdefault(
                (stage, instance),
                {"stage": stage, "instance_id": instance,
                 "completed_jobs": 0, "exact_matched_jobs": 0,
                 "reference_instances": set(), "ratios": [], "peer_samples": 0},
            )
            result["completed_jobs"] += len(timings)
            peers = {
                key: samples for key, samples in instances.items()
                if key != instance and len(samples) >= min_peer_samples
            }
            if not peers:
                continue
            peer_timings = [x for samples in peers.values() for x in samples]
            benchmark = median(peer_timings)
            if benchmark <= 0:
                continue
            result["exact_matched_jobs"] += len(timings)
            result["peer_samples"] += len(peer_timings)
            result["reference_instances"].update(peers)
            result["ratios"].extend(value / benchmark for value in timings)

    assessments: list[dict[str, Any]] = []
    for (_stage, _instance), item in sorted(by_instance.items()):
        enough = (
            item["exact_matched_jobs"] >= min_matches
            and item["peer_samples"] >= min_peer_samples
            and bool(item["reference_instances"])
        )
        ratio = median(item["ratios"]) if enough else None
        state = (
            "confirmed_degraded" if ratio is not None and ratio >= confirmed_ratio
            else "suspected_slow" if ratio is not None and ratio >= suspected_ratio
            else "healthy" if ratio is not None
            else "insufficient_data"
        )
        assessments.append({
            "stage": item["stage"],
            "instance_id": item["instance_id"],
            "state": state,
            "completed_jobs": item["completed_jobs"],
            "exact_matched_jobs": item["exact_matched_jobs"],
            "peer_samples": item["peer_samples"],
            "peer_instances": len(item["reference_instances"]),
            "ratio_to_exact_peers": round(ratio, 3) if ratio is not None else None,
            "confidence": "confirmed" if state == "confirmed_degraded"
            else "provisional" if state == "suspected_slow" else
            "measured" if state == "healthy" else "insufficient",
        })
    return assessments
