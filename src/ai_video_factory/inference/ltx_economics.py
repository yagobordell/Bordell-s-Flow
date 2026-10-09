"""Opt-in, conservative LTX capacity projections; never a Salad invoice.

A warm worker can make progress while new workers are bootstrapping. Models
cannot establish provider allocation delays or actual billing from this data:
the projected cost counts every selected replica for the *entire* forecast
window, including provisioning, and is only useful for comparing choices.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LtxCapacityOption:
    replicas: int
    warm_replicas: int
    cold_replicas: int
    eta_seconds: float | None
    projected_gpu_seconds: float
    projected_usd: float
    meets_deadline: bool
    within_budget: bool


@dataclass(frozen=True, slots=True)
class LtxCapacityDecision:
    selected: LtxCapacityOption
    options: tuple[LtxCapacityOption, ...]
    reason: str


def plan_ltx_capacity(
    *,
    remaining_work_seconds: float,
    active_jobs: int,
    running_jobs: int,
    current_replicas: int,
    max_replicas: int,
    bootstrap_seconds: float,
    target_seconds: float,
    utilization: float,
    hourly_usd: float,
    projected_pool_budget_usd: float | None = None,
) -> LtxCapacityDecision:
    """Compare safe replica counts for one shared LTX pool.

    Budget is a *forward pool projection*, not a per-video guarantee. This
    function never stops running work or publishes draining/provider commands.
    The caller remains responsible for project quota and lifecycle fences.
    """
    for name, value in (
        ("remaining_work_seconds", remaining_work_seconds),
        ("bootstrap_seconds", bootstrap_seconds),
        ("target_seconds", target_seconds),
        ("utilization", utilization),
        ("hourly_usd", hourly_usd),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if not 0 < utilization <= 1 or target_seconds <= 0 or hourly_usd <= 0:
        raise ValueError("Utilization, target and hourly price must be positive")
    if projected_pool_budget_usd is not None and (
        not math.isfinite(projected_pool_budget_usd)
        or projected_pool_budget_usd < 0
    ):
        raise ValueError("Projected pool budget must be finite and nonnegative")
    if min(active_jobs, running_jobs, current_replicas, max_replicas) < 0:
        raise ValueError("Job and replica counts must be nonnegative")
    if running_jobs > active_jobs:
        raise ValueError("Running jobs cannot exceed active jobs")
    if max_replicas < running_jobs:
        raise ValueError("Service capacity is below already-running job count")
    if remaining_work_seconds == 0 or active_jobs == 0:
        idle = LtxCapacityOption(0, 0, 0, 0.0, 0.0, 0.0, True, True)
        return LtxCapacityDecision(idle, (idle,), "no_remaining_work")

    limit = min(active_jobs, max_replicas)
    if not limit:
        paused = LtxCapacityOption(
            0, 0, 0, None, 0.0, 0.0, False, True,
        )
        return LtxCapacityDecision(paused, (paused,), "service_capacity_disabled")
    options: list[LtxCapacityOption] = []
    for replicas in range(max(1, running_jobs), limit + 1):
        warm = min(current_replicas, replicas)
        cold = replicas - warm
        if cold and warm:
            completed_before_start = warm * utilization * bootstrap_seconds
            if remaining_work_seconds <= completed_before_start:
                eta = remaining_work_seconds / (warm * utilization)
            else:
                remaining = remaining_work_seconds - completed_before_start
                eta = bootstrap_seconds + remaining / (replicas * utilization)
        elif cold:
            eta = bootstrap_seconds + remaining_work_seconds / (
                replicas * utilization
            )
        else:
            eta = remaining_work_seconds / (replicas * utilization)
        # Deliberately pessimistic: new replicas may be billed while starting.
        gpu_seconds = replicas * eta
        cost = gpu_seconds * hourly_usd / 3600
        options.append(LtxCapacityOption(
            replicas=replicas,
            warm_replicas=warm,
            cold_replicas=cold,
            eta_seconds=eta,
            projected_gpu_seconds=gpu_seconds,
            projected_usd=cost,
            meets_deadline=eta <= target_seconds,
            within_budget=(
                projected_pool_budget_usd is None
                or cost <= projected_pool_budget_usd
            ),
        ))
    affordable = [option for option in options if option.within_budget]
    if not affordable:
        # Never automatically add a new replica when all projections breach
        # the optional pool budget. Existing running capacity is still managed
        # through the ordinary safety-preserving downscale mechanism.
        existing = [
            option for option in options
            if option.replicas <= max(current_replicas, running_jobs)
        ]
        if not existing:
            # No warm or active replica to preserve. A strict *opt-in* pool
            # budget must not launch the first worker if no plan is affordable.
            paused = LtxCapacityOption(
                0, 0, 0, None, 0.0, 0.0, False, True,
            )
            return LtxCapacityDecision(
                paused, tuple(options), "projected_pool_budget_blocks_new_capacity",
            )
        selected = min(
            existing,
            key=lambda option: (option.projected_usd, option.replicas),
        )
        return LtxCapacityDecision(
            selected, tuple(options), "all_options_over_projected_pool_budget",
        )
    on_time = [option for option in affordable if option.meets_deadline]
    if on_time:
        selected = min(
            on_time,
            key=lambda option: (
                option.projected_usd, option.cold_replicas,
                option.eta_seconds,
            ),
        )
        reason = "lowest_projected_cost_within_deadline"
    else:
        selected = min(
            affordable,
            key=lambda option: (
                option.eta_seconds, option.projected_usd,
                option.cold_replicas,
            ),
        )
        reason = "fastest_affordable_deadline_unreachable"
    return LtxCapacityDecision(selected, tuple(options), reason)
