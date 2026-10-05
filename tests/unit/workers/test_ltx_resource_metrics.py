"""CPU-only regressions for A2V host-memory pressure classification."""

from __future__ import annotations

from typing import Any

import pytest

from ai_video_factory.workers.ltx25.resource_metrics import check_stage1_host_pressure


def _snapshot(**overrides: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "process_major_page_faults": 11050,
        "process_vmswap_bytes": 0,
        "cgroup_available": True,
        "cgroup_version": "v2",
        "cgroup_swap_counter_bytes": 0,
        "cgroup_swap_peak_bytes": 0,
        "cgroup_memory_events_high": 0,
        "cgroup_memory_events_max": 0,
        "cgroup_memory_events_oom": 0,
        "cgroup_memory_events_oom_kill": 0,
        "cgroup_pgscan": 100,
        "cgroup_pgsteal": 80,
    }
    snapshot.update(overrides)
    return snapshot


def test_real_process_swap_growth_aborts_before_stage2() -> None:
    metrics: dict[str, Any] = {}
    with pytest.raises(RuntimeError, match="real host memory pressure"):
        check_stage1_host_pressure(
            _snapshot(),
            _snapshot(
                process_major_page_faults=163104,
                process_vmswap_bytes=64 * 1024 * 1024,
            ),
            0,
            metrics,
            major_page_faults_diagnostic_limit=20000,
        )
    assert metrics["stage1_process_swap_growth_bytes"] == 64 * 1024 * 1024
    assert metrics["stage1_host_pressure_detected"] is True


@pytest.mark.parametrize("event", ["oom", "oom_kill"])
def test_cgroup_oom_events_abort(event: str) -> None:
    before = _snapshot()
    after = _snapshot(process_major_page_faults=163104)
    after[f"cgroup_memory_events_{event}"] = 1
    with pytest.raises(RuntimeError, match=event):
        check_stage1_host_pressure(before, after, 0)


def test_cgroup_high_with_reclaim_aborts_and_preserves_reclaim_telemetry() -> None:
    metrics: dict[str, Any] = {}
    check_after = _snapshot(
        process_major_page_faults=163104,
        cgroup_memory_events_high=1,
        cgroup_pgscan=140,
        cgroup_pgsteal=119,
    )
    with pytest.raises(RuntimeError, match="cgroup_memory_events_high_delta=1"):
        check_stage1_host_pressure(_snapshot(), check_after, 0, metrics)
    assert metrics["stage1_cgroup_reclaim_deltas"] == {
        "pgscan": 40,
        "pgsteal": 39,
    }


def test_reclaim_without_swap_or_pressure_event_is_diagnostic_only() -> None:
    metrics: dict[str, Any] = {}
    check_stage1_host_pressure(
        _snapshot(),
        _snapshot(
            process_major_page_faults=163104,
            cgroup_pgscan=1000,
            cgroup_pgsteal=900,
        ),
        0,
        metrics,
        major_page_faults_diagnostic_limit=20000,
    )
    assert metrics["stage1_cgroup_reclaim_deltas"] == {
        "pgscan": 900,
        "pgsteal": 820,
    }
    assert metrics["stage1_major_page_faults_exceeded_diagnostic"] is True
    assert metrics["stage1_host_pressure_detected"] is False


def test_cgroup_swap_growth_honors_explicit_byte_limit() -> None:
    check_stage1_host_pressure(
        _snapshot(),
        _snapshot(
            process_major_page_faults=163104,
            cgroup_swap_counter_bytes=1024,
            cgroup_swap_peak_bytes=1024,
        ),
        2048,
    )
    with pytest.raises(RuntimeError, match="cgroup_swap_growth_bytes"):
        check_stage1_host_pressure(
            _snapshot(),
            _snapshot(
                process_major_page_faults=163104,
                cgroup_swap_counter_bytes=4096,
                cgroup_swap_peak_bytes=4096,
            ),
            2048,
        )


def test_major_fault_counter_is_never_required_for_pressure_decision() -> None:
    metrics: dict[str, Any] = {}
    check_stage1_host_pressure(
        {"process_vmswap_bytes": 0, "cgroup_available": False},
        {"process_vmswap_bytes": 0, "cgroup_available": False},
        0,
        metrics,
        major_page_faults_diagnostic_limit=20000,
    )
    assert metrics["stage1_major_page_faults_delta"] is None
    assert metrics["stage1_host_pressure_detected"] is False


def test_negative_pressure_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="swap-growth"):
        check_stage1_host_pressure({}, {}, -1)
    with pytest.raises(ValueError, match="major-page-fault"):
        check_stage1_host_pressure(
            {}, {}, 0, major_page_faults_diagnostic_limit=-1
        )
