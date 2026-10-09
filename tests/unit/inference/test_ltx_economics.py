"""Deterministic economics tests; no Salad API, queues or GPU required."""

from __future__ import annotations

import pytest

from ai_video_factory.inference.ltx_economics import plan_ltx_capacity


def _plan(**overrides):
    args = {
        "remaining_work_seconds": 1000.0,
        "active_jobs": 10,
        "running_jobs": 0,
        "current_replicas": 1,
        "max_replicas": 4,
        "bootstrap_seconds": 1400.0,
        "target_seconds": 3600.0,
        "utilization": 0.85,
        "hourly_usd": 0.50,
    }
    args.update(overrides)
    return plan_ltx_capacity(**args)


def test_warm_worker_finishes_before_slow_cold_worker_is_ready():
    decision = _plan(remaining_work_seconds=500.0, active_jobs=4)
    assert decision.selected.replicas == 1
    assert decision.selected.cold_replicas == 0
    assert decision.selected.meets_deadline


def test_large_backlog_selects_more_workers_to_meet_deadline():
    decision = _plan(
        remaining_work_seconds=12000.0, active_jobs=52,
        bootstrap_seconds=900.0,
    )
    assert decision.selected.replicas > 1
    assert decision.selected.meets_deadline


def test_budget_prevents_uneconomic_expansion():
    decision = _plan(
        remaining_work_seconds=12000.0,
        target_seconds=1200.0,
        projected_pool_budget_usd=0.01,
    )
    assert decision.selected.replicas == 1
    assert decision.reason == "all_options_over_projected_pool_budget"


def test_running_job_capacity_never_removed_by_projection():
    decision = _plan(
        active_jobs=2, running_jobs=2, current_replicas=3, max_replicas=4,
    )
    assert decision.selected.replicas == 2
    assert all(option.replicas >= 2 for option in decision.options)


def test_zero_demand_scales_to_zero_even_with_no_worker():
    decision = _plan(
        remaining_work_seconds=0, active_jobs=0, current_replicas=0,
    )
    assert decision.selected.replicas == 0
    assert decision.reason == "no_remaining_work"


def test_cold_start_is_included_in_eta_and_conservative_cost():
    decision = _plan(
        remaining_work_seconds=5000, current_replicas=0,
        bootstrap_seconds=1400,
    )
    for option in decision.options:
        assert option.eta_seconds >= 1400
        assert option.projected_gpu_seconds == pytest.approx(
            option.replicas * option.eta_seconds
        )


@pytest.mark.parametrize("bad", [
    {"utilization": 0},
    {"target_seconds": 0},
    {"remaining_work_seconds": float("nan")},
    {"hourly_usd": float("inf")},
    {"running_jobs": 3, "active_jobs": 2},
    {"max_replicas": 1, "running_jobs": 2},
])
def test_invalid_projections_fail_closed(bad):
    with pytest.raises(ValueError):
        _plan(**bad)
