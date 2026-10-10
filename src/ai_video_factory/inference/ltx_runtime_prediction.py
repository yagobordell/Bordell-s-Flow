"""Conservative per-job LTX runtime estimates from comparable warm jobs.

No GPU requests or quality changes are made here. Predictions are observations,
not deadlines, and fall back to existing stage-level history when unsupported.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any

from .ltx_performance_health import assess_ltx_instance_health


def _positive(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _shape(raw: object) -> int | None:
    value = _positive(raw)
    if value is None:
        return None
    return math.ceil(value / 64) * 64


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(math.ceil(len(ordered) * fraction) - 1, len(ordered) - 1)]


def forecast_ltx_job_seconds(
    job: dict[str, object],
    history: list[dict[str, Any]],
    *,
    stage: str,
    fallback: float,
    minimum_samples: int = 3,
    percentile: float = 0.75,
    excluded_instances: frozenset[str] = frozenset(),
) -> float:
    """Predict by exact stage/profile/padded geometry/frames, then per-frame scale.

    Extrapolation is limited to observed geometries of the same generation
    profile. Only completed warm attempts with trusted Salad identities count.
    """
    if minimum_samples < 2 or not 0.5 <= percentile <= 1:
        raise ValueError("Invalid LTX runtime forecast thresholds")
    base = _positive(fallback)
    if base is None:
        raise ValueError("A positive fallback is mandatory")
    try:
        profile = str(job["generation_profile"])
        width = _shape(job["width"])
        height = _shape(job["height"])
        frames = int(job["num_frames"])
    except (KeyError, ValueError, TypeError, OverflowError):
        return base
    if not profile or not width or not height or frames < 1:
        return base
    matched: list[tuple[int, float]] = []
    for item in history:
        try:
            item_frames = int(item["num_frames"])
            item_width, item_height = int(item["width"]), int(item["height"])
            runtime = _positive(item.get("task_seconds"))
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        if (
            item.get("stage") != stage
            or item.get("profile") != profile
            or item_width != width or item_height != height
            or item_frames < 1 or runtime is None
            or item.get("pipeline_reused") is not True
            or item.get("instance_identity_source") != "salad_instance"
            or item.get("instance_id") in excluded_instances
        ):
            continue
        matched.append((item_frames, runtime))

    exact = [runtime for n, runtime in matched if n == frames]
    if len(exact) >= minimum_samples:
        return max(_percentile(exact, percentile), 1.0)

    # Only nearby, physically comparable clips can inform an approximate ETA.
    nearby = [
        runtime / n * frames for n, runtime in matched
        if frames / 2 <= n <= frames * 2
    ]
    if len(nearby) >= max(minimum_samples + 2, 5):
        return max(_percentile(nearby, percentile), 1.0)
    return base


def degraded_instance_ids(
    observations: list[dict[str, Any]], *, now: object = None,
) -> frozenset[str]:
    """Exclude validated slow instances before learning a normal pool runtime."""
    kwargs = {"now": now} if now is not None else {}
    return frozenset(
        str(row["instance_id"])
        for row in assess_ltx_instance_health(observations, **kwargs)
        if row["state"] == "confirmed_degraded"
    )


def typical_forecast_seconds(rows: list[dict[str, object]], fallback: float) -> float:
    """Stage-level display value only; capacity uses individual row estimates."""
    values = [
        v for row in rows
        if (v := _positive(row.get("forecast_runtime_seconds"))) is not None
    ]
    return median(values) if values else fallback
