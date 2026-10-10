"""Durable, backwards-compatible run-session accounting for pipeline resumptions.

Session durations are wall clock for each invocation, not sums of concurrent
API or GPU work. Cost snapshots are *observed or estimated*, never invoices.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _time_iso(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).astimezone(UTC).isoformat()


def _nonnegative(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    return round(value, 3) if math.isfinite(value) and value >= 0 else None


def _state(report: dict[str, Any]) -> dict[str, Any]:
    saved = report.get("execution_sessions")
    if isinstance(saved, dict) and saved.get("schema_version") == 1:
        return saved
    previous = report.get("timings")
    elapsed = (
        _nonnegative(previous.get("run_elapsed_seconds"))
        if isinstance(previous, dict) else None
    )
    old_items = [{
        "id": "legacy_run", "type": "historical",
        "status": "historical_unattributed",
        "started_at": None, "finished_at": None,
        "elapsed_seconds": elapsed,
        "stage_seconds": {},
        "estimated_api_cost_usd": None, "observed_salad_cost_usd": None,
        "billed_cost_usd": None,
    }] if previous is not None else []
    saved = {"schema_version": 1, "items": old_items, "cumulative_elapsed_seconds": elapsed}
    report["execution_sessions"] = saved
    return saved


def begin_run_session(
    report: dict[str, Any], *, mode: str, at: datetime | None = None,
) -> str:
    """Record session start before any lengthy provider calls."""
    saved = _state(report)
    for entry in saved["items"]:
        if entry["status"] == "running":
            entry["status"] = "interrupted"
            entry["finished_at"] = None
            entry["elapsed_seconds"] = None
    identifier = uuid4().hex
    saved["items"].append({
        "id": identifier, "type": mode, "status": "running",
        "started_at": _time_iso(at), "finished_at": None,
        "elapsed_seconds": None, "stage_seconds": {},
        "estimated_api_cost_usd": None, "observed_salad_cost_usd": None,
        "billed_cost_usd": None,
    })
    return identifier


def finish_run_session(
    report: dict[str, Any], identifier: str, *,
    elapsed_seconds: float, status: str, stages: dict[str, float] | None = None,
    at: datetime | None = None,
) -> None:
    """Finalize idempotently; the same session cannot add duration twice."""
    if status not in {"completed", "failed"}:
        raise ValueError("Invalid run session status")
    saved = _state(report)
    matches = [entry for entry in saved["items"] if entry["id"] == identifier]
    if len(matches) != 1:
        raise ValueError("Unknown run session")
    entry = matches[0]
    if entry["status"] != "running":
        raise ValueError("Run session already finalized")
    duration = _nonnegative(elapsed_seconds)
    if duration is None:
        raise ValueError("Invalid session duration")
    clean_stages = {
        key: measured for name, raw in (stages or {}).items()
        if isinstance(name, str) and (measured := _nonnegative(raw)) is not None
        for key in [name]
    }
    entry.update(
        status=status, finished_at=_time_iso(at), elapsed_seconds=duration,
        stage_seconds=clean_stages,
    )
    costs = report.get("api_costs")
    if isinstance(costs, dict):
        entry["estimated_api_cost_usd"] = costs.get("priced_subtotal_usd")
    salad = report.get("salad_costs")
    if isinstance(salad, dict):
        # Cumulative Salad snapshots are not per-session invoices. Preserve
        # them at the session level, but label them explicitly.
        entry["observed_salad_cost_usd"] = salad.get("observed_total_usd")
    saved["cumulative_elapsed_seconds"] = round(sum(
        item["elapsed_seconds"] for item in saved["items"]
        if isinstance(item.get("elapsed_seconds"), (int, float))
    ), 3)
    saved["unmeasured_sessions"] = sum(
        item.get("elapsed_seconds") is None for item in saved["items"]
    )
