from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_video_factory.workflows.execution_sessions import (
    begin_run_session,
    finish_run_session,
)

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def test_legacy_report_survives_and_resume_accumulates_elapsed_without_overlap() -> None:
    report = {"timings": {"run_elapsed_seconds": 140.0}}
    first = begin_run_session(report, mode="videos-only", at=NOW)
    finish_run_session(
        report, first, elapsed_seconds=360.25, status="completed",
        stages={"LTX video session": 360.25, "B2.2": 5.2},
        at=NOW + timedelta(minutes=6),
    )
    saved = report["execution_sessions"]
    assert saved["schema_version"] == 1
    assert saved["items"][0]["status"] == "historical_unattributed"
    assert saved["items"][0]["elapsed_seconds"] == 140
    assert saved["items"][1]["stage_seconds"]["B2.2"] == 5.2
    assert saved["cumulative_elapsed_seconds"] == 500.25
    with pytest.raises(ValueError, match="already finalized"):
        finish_run_session(report, first, elapsed_seconds=100, status="completed")


def test_crashed_session_is_preserved_unknown_and_not_double_counted() -> None:
    report = {"timings": {"run_elapsed_seconds": 10.0}}
    interrupted = begin_run_session(report, mode="videos-only", at=NOW)
    resumed = begin_run_session(
        report, mode="videos-only", at=NOW + timedelta(hours=1),
    )
    finish_run_session(
        report, resumed, elapsed_seconds=20.0, status="failed",
    )
    entries = report["execution_sessions"]["items"]
    assert any(
        entry["id"] == interrupted and entry["status"] == "interrupted"
        and entry["elapsed_seconds"] is None for entry in entries
    )
    assert report["execution_sessions"]["cumulative_elapsed_seconds"] == 30.0
    assert report["execution_sessions"]["unmeasured_sessions"] == 1


def test_unknown_expenses_remain_none_instead_of_claimed_zero() -> None:
    report = {}
    identifier = begin_run_session(report, mode="videos-only")
    finish_run_session(report, identifier, elapsed_seconds=1, status="completed")
    latest = report["execution_sessions"]["items"][-1]
    assert latest["billed_cost_usd"] is None
    assert latest["estimated_api_cost_usd"] is None
    assert latest["observed_salad_cost_usd"] is None
