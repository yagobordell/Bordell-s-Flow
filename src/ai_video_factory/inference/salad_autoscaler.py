from __future__ import annotations

import math
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from .coordination import acquire_instance_drain_lock
from .salad import SaladClient

ACTIVE_STATUSES = (
    "pending",
    "retryable_failed",
    "running",
)

# LTX scales by backlog count rather than the generic drain-time forecast.
# Each replica still consumes the shared Postgres queue dynamically.
_DEFAULT_JOBS_PER_REPLICA = {
    "ltx25_i2v": 1,
    "ltx25": 1,
}

_PROVIDER_STABLE_STATUSES = {"running", "stopped"}
_PROVIDER_TRANSITION_STATUSES = {"pending", "deploying"}
_PROVIDER_BOOTSTRAP_STATE_RANK = {
    "allocating": 1, "downloading": 2, "creating": 3, "running": 4,
}
_PROVIDER_TERMINAL_STATUSES = {"failed", "succeeded"}
_PROVIDER_KNOWN_STATUSES = (
    _PROVIDER_STABLE_STATUSES
    | _PROVIDER_TRANSITION_STATUSES
    | _PROVIDER_TERMINAL_STATUSES
)


class AutoscalerReconciliationError(RuntimeError):
    """A reconcile pass could not safely converge Salad capacity."""


@dataclass(frozen=True, slots=True)
class AutoscalerServiceBinding:
    workload: str
    task_names: tuple[str, ...]
    autoscaler_env_prefix: str
    fallback_runtime_seconds: float
    max_replicas: int


@dataclass(frozen=True, slots=True)
class PredictiveAutoscalerConfig:
    enabled: bool
    project_max_replicas: int
    target_drain_seconds: float
    cold_start_seconds: float
    target_utilization: float
    runtime_percentile: float
    runtime_history_limit: int
    downscale_stable_polls: int
    max_upscale_per_poll: int
    max_downscale_per_poll: int
    active_deletion_cost: int
    idle_deletion_cost: int
    stage_max_replicas: dict[str, int]
    fallback_runtime_seconds: dict[str, float]
    stage_cold_start_seconds: dict[str, float] | None = None
    stage_jobs_per_replica: dict[str, int] | None = None
    zero_demand_stable_polls: int = 1
    drain_grace_seconds: float = 5.0
    drain_ttl_seconds: float = 120.0
    nonconvergence_failure_polls: int = 4
    provider_pending_max_seconds: float = 7200.0


@dataclass(frozen=True, slots=True)
class StageDemand:
    stage: str
    rows: tuple[dict[str, object], ...]
    status_counts: dict[str, int]
    run_counts: dict[str, int]
    expected_runtime_seconds: float
    remaining_work_seconds: float
    running_count: int
    forecast_blocked_count: int
    ideal_replicas: int
    jobs_per_replica: int | None = None
    job_count_target: int | None = None


@dataclass(frozen=True, slots=True)
class AutoscaleResult:
    stage: str
    current_replicas: int
    target_replicas: int
    applied_replicas: int
    changed: bool
    demand: StageDemand
    reason: str


def next_reconcile_delay_seconds(
    results: Mapping[str, AutoscaleResult],
    *,
    poll_seconds: float,
    drain_grace_seconds: float,
) -> float:
    """Shorten only the safe follow-up poll after zero-demand drain publication."""

    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if drain_grace_seconds < 0:
        raise ValueError("drain_grace_seconds must be non-negative")

    zero_demand_drain_pending = any(
        result.target_replicas == 0
        and result.demand.remaining_work_seconds <= 0
        and result.reason == "drain_grace_pending"
        for result in results.values()
    )
    if not zero_demand_drain_pending:
        return poll_seconds
    return min(poll_seconds, max(drain_grace_seconds, 0.1))


def publish_instance_drains(
    connection: object,
    *,
    stage: str,
    instance_ids: tuple[str, ...],
    ttl_seconds: float,
    hold_until_confirmed: bool = False,
) -> None:
    """Publish drain intents while holding the same per-instance lock used by claims."""

    for instance_id in instance_ids:
        acquire_instance_drain_lock(connection, instance_id)
        connection.execute(
            """
            INSERT INTO gpu.capacity_drains (
                service,
                instance_id,
                requested_at,
                expires_at,
                hold_until_confirmed
            ) VALUES (
                %s,
                %s,
                now(),
                now() + (%s * interval '1 second'),
                %s
            )
            ON CONFLICT (service, instance_id) DO UPDATE
            SET expires_at = EXCLUDED.expires_at,
                hold_until_confirmed = EXCLUDED.hold_until_confirmed
            """,
            (stage, instance_id, ttl_seconds, hold_until_confirmed),
        )


class PostgresAutoscalerStore:
    """Global demand/runtime view over the canonical ``gpu.jobs`` control plane."""

    def __init__(self, dsn: str, *, max_connections: int = 2) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=0,
            max_size=max_connections,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )

    def list_stage_jobs(
        self,
        *,
        binding: AutoscalerServiceBinding,
        statuses: tuple[str, ...],
    ) -> list[dict[str, object]]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    job_id,
                    COALESCE(
                        request ->> 'run_id',
                        request #>> '{parameters,run_id}',
                        '<unknown>'
                    ) AS run_id,
                    status,
                    lease_owner AS worker_id,
                    started_at
                FROM gpu.jobs
                WHERE task = ANY(%s)
                  AND status = ANY(%s)
                ORDER BY created_at ASC, job_id ASC
                """,
                (list(binding.task_names), list(statuses)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_stage_runtime_seconds(
        self,
        *,
        binding: AutoscalerServiceBinding,
        limit: int,
    ) -> list[float]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT EXTRACT(EPOCH FROM (finished_at - started_at)) AS runtime_seconds
                FROM gpu.jobs
                WHERE task = ANY(%s)
                  AND status = 'succeeded'
                  AND started_at IS NOT NULL
                  AND finished_at IS NOT NULL
                  AND finished_at > started_at
                ORDER BY finished_at DESC
                LIMIT %s
                """,
                (list(binding.task_names), limit),
            ).fetchall()
        return [float(row["runtime_seconds"]) for row in rows]

    def list_draining_instances(self, *, stage: str) -> dict[str, datetime]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT instance_id, requested_at
                FROM gpu.capacity_drains
                WHERE service = %s
                  AND (expires_at > now() OR hold_until_confirmed)
                """,
                (stage,),
            ).fetchall()
        return {
            str(row["instance_id"]): _parse_datetime(row["requested_at"]) or datetime.now(UTC)
            for row in rows
        }

    def mark_draining_instances(
        self,
        *,
        stage: str,
        instance_ids: tuple[str, ...],
        ttl_seconds: float,
    ) -> None:
        if not instance_ids:
            return
        with self._pool.connection() as connection, connection.transaction():
            publish_instance_drains(
                connection,
                stage=stage,
                instance_ids=instance_ids,
                ttl_seconds=ttl_seconds,
            )

    def hold_draining_instances(
        self,
        *,
        stage: str,
        instance_ids: tuple[str, ...],
    ) -> None:
        if not instance_ids:
            return
        with self._pool.connection() as connection, connection.transaction():
            for instance_id in instance_ids:
                acquire_instance_drain_lock(connection, instance_id)
                updated = connection.execute(
                    """
                    UPDATE gpu.capacity_drains
                    SET hold_until_confirmed = true
                    WHERE service = %s
                      AND instance_id = %s
                    """,
                    (stage, instance_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        f"missing drain row while promoting provider hold: {stage}/{instance_id}"
                    )

    def clear_draining_instances(
        self,
        *,
        stage: str,
        keep_instance_ids: tuple[str, ...] = (),
    ) -> None:
        with self._pool.connection() as connection:
            if keep_instance_ids:
                connection.execute(
                    """
                    DELETE FROM gpu.capacity_drains
                    WHERE service = %s
                      AND (
                          (expires_at <= now() AND NOT hold_until_confirmed)
                          OR NOT (instance_id = ANY(%s))
                      )
                    """,
                    (stage, list(keep_instance_ids)),
                )
            else:
                connection.execute(
                    "DELETE FROM gpu.capacity_drains WHERE service = %s",
                    (stage,),
                )

    def ping(self) -> None:
        with self._pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def close(self) -> None:
        self._pool.close()


def load_predictive_autoscaler_config(
    *,
    stage_bindings: tuple[AutoscalerServiceBinding, ...],
) -> PredictiveAutoscalerConfig:
    project_max = _int_env("SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS", 30, minimum=1)
    ltx_stage_count = sum(
        binding.workload in _DEFAULT_JOBS_PER_REPLICA for binding in stage_bindings
    )
    required_ltx_project_capacity = 2 * ltx_stage_count
    if required_ltx_project_capacity and project_max < required_ltx_project_capacity:
        raise RuntimeError(
            "SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS must be >= "
            f"{required_ltx_project_capacity} to preserve two-way capacity for "
            f"{ltx_stage_count} production LTX service(s)"
        )
    cold_start_seconds = _float_env(
        "SALAD_AUTOSCALER_COLD_START_SECONDS",
        180.0,
        minimum=0.0,
    )
    stage_max: dict[str, int] = {}
    fallback_runtime: dict[str, float] = {}
    stage_cold_start: dict[str, float] = {}
    stage_jobs_per_replica: dict[str, int] = {}
    for binding in stage_bindings:
        configured_max = _int_env(
            f"SALAD_AUTOSCALER_{binding.autoscaler_env_prefix}_MAX_REPLICAS",
            binding.max_replicas,
            minimum=0,
        )
        if binding.workload in _DEFAULT_JOBS_PER_REPLICA and configured_max < 2:
            raise RuntimeError(
                f"{binding.workload} requires at least 2 configured replicas "
                "to preserve two-way run generation"
            )
        stage_max[binding.workload] = min(configured_max, binding.max_replicas)
        fallback_runtime[binding.workload] = _float_env(
            f"SALAD_AUTOSCALER_{binding.autoscaler_env_prefix}_RUNTIME_SECONDS",
            binding.fallback_runtime_seconds,
            minimum=1.0,
        )
        stage_cold_start[binding.workload] = _float_env(
            f"SALAD_AUTOSCALER_{binding.autoscaler_env_prefix}_COLD_START_SECONDS",
            cold_start_seconds,
            minimum=0.0,
        )
        default_jobs_per_replica = _DEFAULT_JOBS_PER_REPLICA.get(binding.workload)
        if default_jobs_per_replica is not None:
            configured_jobs_per_replica = _int_env(
                f"SALAD_AUTOSCALER_{binding.autoscaler_env_prefix}_JOBS_PER_REPLICA",
                default_jobs_per_replica,
                minimum=1,
            )
            if configured_jobs_per_replica != 1:
                raise RuntimeError(
                    f"{binding.workload} requires jobs_per_replica=1 "
                    "to preserve two-way run generation"
                )
            stage_jobs_per_replica[binding.workload] = 1
    return PredictiveAutoscalerConfig(
        enabled=_bool_env("SALAD_AUTOSCALER_ENABLED", default=False),
        project_max_replicas=project_max,
        target_drain_seconds=_float_env(
            "SALAD_AUTOSCALER_TARGET_DRAIN_MINUTES",
            20.0,
            minimum=1.0,
        )
        * 60.0,
        cold_start_seconds=cold_start_seconds,
        target_utilization=_bounded_float_env(
            "SALAD_AUTOSCALER_TARGET_UTILIZATION",
            0.85,
            minimum=0.1,
            maximum=1.0,
        ),
        runtime_percentile=_bounded_float_env(
            "SALAD_AUTOSCALER_RUNTIME_PERCENTILE",
            0.75,
            minimum=0.5,
            maximum=1.0,
        ),
        runtime_history_limit=_int_env(
            "SALAD_AUTOSCALER_RUNTIME_HISTORY_JOBS",
            200,
            minimum=1,
        ),
        downscale_stable_polls=_int_env(
            "SALAD_AUTOSCALER_DOWNSCALE_STABLE_POLLS",
            2,
            minimum=1,
        ),
        zero_demand_stable_polls=_int_env(
            "SALAD_AUTOSCALER_ZERO_DEMAND_STABLE_POLLS",
            1,
            minimum=1,
        ),
        nonconvergence_failure_polls=_int_env(
            "SALAD_AUTOSCALER_NONCONVERGENCE_FAILURE_POLLS",
            4,
            minimum=2,
        ),
        max_upscale_per_poll=_int_env(
            "SALAD_AUTOSCALER_MAX_UPSCALE_PER_POLL",
            10,
            minimum=1,
        ),
        max_downscale_per_poll=_int_env(
            "SALAD_AUTOSCALER_MAX_DOWNSCALE_PER_POLL",
            10,
            minimum=1,
        ),
        active_deletion_cost=_int_env(
            "SALAD_AUTOSCALER_ACTIVE_DELETION_COST",
            100_000,
            minimum=1,
        ),
        idle_deletion_cost=_int_env(
            "SALAD_AUTOSCALER_IDLE_DELETION_COST",
            0,
            minimum=0,
        ),
        stage_max_replicas=stage_max,
        fallback_runtime_seconds=fallback_runtime,
        stage_cold_start_seconds=stage_cold_start,
        stage_jobs_per_replica=stage_jobs_per_replica,
        drain_grace_seconds=_float_env(
            "SALAD_AUTOSCALER_DRAIN_GRACE_SECONDS",
            5.0,
            minimum=0.0,
        ),
        drain_ttl_seconds=_float_env(
            "SALAD_AUTOSCALER_DRAIN_TTL_SECONDS",
            120.0,
            minimum=5.0,
        ),
        provider_pending_max_seconds=_float_env(
            "SALAD_AUTOSCALER_PROVIDER_PENDING_MAX_SECONDS",
            7200.0,
            minimum=60.0,
        ),
    )


class PredictiveSaladAutoscaler:
    """Media Pipeline predictive autoscaling policy over Bordell's Postgres jobs."""

    def __init__(
        self,
        *,
        config: PredictiveAutoscalerConfig,
        store: PostgresAutoscalerStore,
        clients: dict[str, SaladClient],
        bindings: dict[str, AutoscalerServiceBinding],
        managed_group_names: set[str] | None = None,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.store = store
        self.clients = dict(clients)
        self.bindings = dict(bindings)
        self.managed_group_names = (
            frozenset(managed_group_names) if managed_group_names is not None else None
        )
        self.logger = logger
        self._downscale_candidates: dict[str, tuple[int, int]] = {}
        self._nonconvergence_candidates: dict[str, tuple[int, int, str]] = {}
        self._provider_transition_first_seen: dict[str, datetime] = {}
        self._provider_bootstrap_progress: dict[
            str, tuple[dict[str, tuple[int | None, int, bool, float | None]], datetime]
        ] = {}
        self._last_log_snapshot: dict[str, tuple[object, ...]] = {}

    def reconcile(self) -> dict[str, AutoscaleResult]:
        if not self.config.enabled or not self.clients:
            return {}
        self._assert_project_inventory_owned()
        rows_by_stage = {
            stage: self.store.list_stage_jobs(
                binding=self.bindings[stage],
                statuses=ACTIVE_STATUSES,
            )
            for stage in self.clients
        }
        runtime_by_stage = {
            stage: _runtime_percentile(
                self.store.list_recent_stage_runtime_seconds(
                    binding=self.bindings[stage],
                    limit=self.config.runtime_history_limit,
                ),
                percentile=self.config.runtime_percentile,
                fallback=self.config.fallback_runtime_seconds[stage],
            )
            for stage in self.clients
        }
        demands = build_global_demands(
            rows_by_stage=rows_by_stage,
            runtime_by_stage=runtime_by_stage,
            config=self.config,
        )
        targets = allocate_project_quota(
            demands=demands,
            project_max_replicas=self.config.project_max_replicas,
        )
        groups = {
            stage: self.clients[stage].describe_container_group() for stage in self.clients
        }
        group_status = {stage: _group_status(groups[stage]) for stage in self.clients}
        group_pending_change = {
            stage: bool(groups[stage].get("pending_change")) for stage in self.clients
        }
        group_transitioning: dict[str, bool] = {}
        transition_instances: dict[str, list[dict[str, object]]] = {}
        observed_at = datetime.now(UTC)
        for stage in self.clients:
            status = group_status[stage]
            if status not in _PROVIDER_KNOWN_STATUSES:
                raise AutoscalerReconciliationError(
                    f"Salad provider state for {stage} is unknown: {status!r}"
                )
            if status in _PROVIDER_TERMINAL_STATUSES:
                raise AutoscalerReconciliationError(
                    f"Salad provider state for {stage} is terminal: {status!r}"
                )

            transitioning = (
                group_pending_change[stage]
                or status in _PROVIDER_TRANSITION_STATUSES
            )
            group_transitioning[stage] = transitioning
            if not transitioning:
                self._provider_transition_first_seen.pop(stage, None)
                self._provider_bootstrap_progress.pop(stage, None)
                continue

            # Always account for in-flight capacity before considering a provider
            # transition healthy. Reuse this snapshot when reserving project quota.
            try:
                transition_instances[stage] = (
                    self.clients[stage].list_container_group_instances()
                )
            except Exception as exc:
                raise AutoscalerReconciliationError(
                    "Salad capacity reconciliation cannot safely account for an "
                    f"active provider transition on {stage}: {exc}"
                ) from exc

            provider_updated_at = _parse_datetime(groups[stage].get("update_time"))
            transition_since = provider_updated_at
            if transition_since is None:
                transition_since = self._provider_transition_first_seen.setdefault(
                    stage,
                    observed_at,
                )
            if not group_pending_change[stage] and status == "deploying":
                # Salad's group update_time can predate a freshly allocated
                # instance by hours. Only *observable forward* bootstrap progress
                # refreshes this watchdog; a stalled download still times out.
                last_progress = self._record_provider_bootstrap_progress(
                    stage,
                    instances=transition_instances[stage],
                    group_version=_optional_int(groups[stage].get("version")),
                    observed_at=observed_at,
                )
                if last_progress is not None:
                    transition_since = max(transition_since, last_progress)
            else:
                self._provider_bootstrap_progress.pop(stage, None)
            transition_age_seconds = max(
                (observed_at - transition_since).total_seconds(),
                0.0,
            )
            if transition_age_seconds > self.config.provider_pending_max_seconds:
                raise AutoscalerReconciliationError(
                    f"Salad provider transition for {stage} has remained active for "
                    f"{transition_age_seconds:.0f}s, exceeding "
                    f"{self.config.provider_pending_max_seconds:.0f}s"
                )

        current: dict[str, int] = {}
        for stage in self.clients:
            configured = max(int(groups[stage].get("replicas") or 0), 0)
            if group_transitioning[stage]:
                live_instances = len(transition_instances[stage])
                current[stage] = max(configured, live_instances)
                continue
            if group_status[stage] == "stopped":
                current[stage] = 0
                continue
            current[stage] = configured
        initial_current = dict(current)
        results: dict[str, AutoscaleResult] = {}
        hard_failures: dict[str, str] = {}

        # Preserve Media Pipeline ordering: only confirmed capacity releases can fund new capacity.
        for stage in self.clients:
            target = targets[stage]
            if group_transitioning[stage]:
                pending_drains = tuple(self.store.list_draining_instances(stage=stage))
                if pending_drains:
                    self.store.hold_draining_instances(
                        stage=stage,
                        instance_ids=pending_drains,
                    )
                results[stage] = AutoscaleResult(
                    stage=stage,
                    current_replicas=current[stage],
                    target_replicas=target,
                    applied_replicas=current[stage],
                    changed=False,
                    demand=demands[stage],
                    reason="provider_change_pending",
                )
                continue
            if target >= current[stage]:
                self._downscale_candidates.pop(stage, None)
                self._nonconvergence_candidates.pop(stage, None)
                self.store.clear_draining_instances(stage=stage)
                continue
            stable_count = self._record_downscale_candidate(stage, target)
            stable_polls_required = self.config.downscale_stable_polls
            if target == 0 and demands[stage].remaining_work_seconds <= 0:
                stable_polls_required = self.config.zero_demand_stable_polls
            if stable_count < stable_polls_required:
                continue
            applied_target = max(
                target,
                current[stage] - self.config.max_downscale_per_poll,
            )
            protected, reason = self._prepare_drained_downscale(
                stage=stage,
                rows=list(demands[stage].rows),
                remove_count=current[stage] - applied_target,
                current_replicas=current[stage],
                expected_runtime_seconds=demands[stage].expected_runtime_seconds,
            )
            if not protected:
                results[stage] = AutoscaleResult(
                    stage=stage,
                    current_replicas=current[stage],
                    target_replicas=target,
                    applied_replicas=current[stage],
                    changed=False,
                    demand=demands[stage],
                    reason=reason,
                )
                if reason == "drain_protection_failed":
                    hard_failures[stage] = reason
                    self._nonconvergence_candidates.pop(stage, None)
                elif reason in {
                    "active_instance_mapping_incomplete",
                    "instance_inventory_incomplete",
                }:
                    count = self._record_nonconvergence(stage, target, reason)
                    if count >= self.config.nonconvergence_failure_polls:
                        hard_failures[stage] = (
                            f"{reason} persisted for {count} reconciliation polls"
                        )
                else:
                    self._nonconvergence_candidates.pop(stage, None)
                continue
            self._nonconvergence_candidates.pop(stage, None)
            provider_drains = tuple(self.store.list_draining_instances(stage=stage))
            if not provider_drains:
                raise AutoscalerReconciliationError(
                    f"Salad downscale for {stage} has no drain rows to hold"
                )
            self.store.hold_draining_instances(
                stage=stage,
                instance_ids=provider_drains,
            )
            if applied_target == 0:
                self.clients[stage].stop_container_group()
                group_status[stage] = "stop_requested"
                results[stage] = AutoscaleResult(
                    stage=stage,
                    current_replicas=current[stage],
                    target_replicas=target,
                    applied_replicas=current[stage],
                    changed=False,
                    demand=demands[stage],
                    reason="stop_requested_pending_confirmation",
                )
                continue
            self.clients[stage].set_container_group_replicas(applied_target)
            results[stage] = AutoscaleResult(
                stage=stage,
                current_replicas=current[stage],
                target_replicas=target,
                applied_replicas=current[stage],
                changed=False,
                demand=demands[stage],
                reason="resize_requested_pending_confirmation",
            )

        available = max(
            self.config.project_max_replicas - sum(current.values()),
            0,
        )
        for stage in self.clients:
            if group_transitioning[stage]:
                continue
            target = targets[stage]
            before = current[stage]
            if target > before and available > 0:
                increase = min(
                    target - before,
                    self.config.max_upscale_per_poll,
                    available,
                )
                if increase > 0:
                    desired = before + increase
                    if group_status[stage] == "stopped":
                        configured = max(int(groups[stage].get("replicas") or 0), 0)
                        if configured != desired:
                            self.clients[stage].set_container_group_replicas(desired)
                            current[stage] = desired
                            available -= increase
                            results[stage] = AutoscaleResult(
                                stage=stage,
                                current_replicas=before,
                                target_replicas=target,
                                applied_replicas=before,
                                changed=False,
                                demand=demands[stage],
                                reason="resize_before_start_pending_confirmation",
                            )
                            continue
                        self.clients[stage].start_container_group_if_needed(
                            warning_logger=lambda _message: None,
                        )
                        group_status[stage] = "start_requested"
                    else:
                        self.clients[stage].set_container_group_replicas(desired)
                    current[stage] = desired
                    available -= increase

        for stage in self.clients:
            previous = initial_current[stage]
            result = results.get(stage) or AutoscaleResult(
                stage=stage,
                current_replicas=previous,
                target_replicas=targets[stage],
                applied_replicas=current[stage],
                changed=current[stage] != previous,
                demand=demands[stage],
                reason="reconciled" if current[stage] != previous else "stable",
            )
            results[stage] = result
            self._log_result(result)
        if hard_failures:
            failed = "; ".join(
                f"{stage}: {reason}" for stage, reason in sorted(hard_failures.items())
            )
            raise AutoscalerReconciliationError(
                "Salad capacity reconciliation could not safely converge: "
                f"{failed}"
            )
        return results

    def _assert_project_inventory_owned(self) -> None:
        if self.managed_group_names is None:
            return
        inventory_client = next(iter(self.clients.values()))
        groups = inventory_client.list_project_container_groups()
        remote_names = {
            str(group.get("name") or "").strip()
            for group in groups
            if str(group.get("name") or "").strip()
        }
        unmanaged = sorted(remote_names - self.managed_group_names)
        if unmanaged:
            raise AutoscalerReconciliationError(
                "Salad project contains unmanaged container groups outside "
                f"deploy/salad/services.json: {', '.join(unmanaged)}"
            )

    def _record_provider_bootstrap_progress(
        self,
        stage: str,
        *,
        instances: list[dict[str, object]],
        group_version: int | None,
        observed_at: datetime,
    ) -> datetime | None:
        """Track actual instance progress, not an unrelated group update_time."""
        current: dict[str, tuple[int | None, int, bool, float | None]] = {}
        for instance in instances:
            instance_id = str(instance.get("id") or "").strip()
            version = _optional_int(instance.get("version"))
            if group_version is not None and version not in (None, group_version):
                continue
            rank = _PROVIDER_BOOTSTRAP_STATE_RANK.get(
                str(instance.get("state") or "").strip().lower(), 0
            )
            if not instance_id or rank == 0:
                continue
            try:
                raw_progress = instance.get("pulling_progress")
                progress = float(raw_progress) if raw_progress is not None else None
            except (TypeError, ValueError, OverflowError):
                progress = None
            if progress is not None and not math.isfinite(progress):
                progress = None
            current[instance_id] = (
                version, rank, instance.get("started") is True, progress,
            )

        previous = self._provider_bootstrap_progress.get(stage)
        if previous is None:
            if not current:
                return None
            last_progress = observed_at
        else:
            prior, last_progress = previous
            for instance_id, (version, rank, started, progress) in current.items():
                old = prior.get(instance_id)
                if (
                    old is None
                    or (version is not None and version != old[0])
                    or rank > old[1]
                    or (started and not old[2])
                    or (
                        progress is not None
                        and (old[3] is None or progress > old[3] + 0.0001)
                    )
                ):
                    last_progress = observed_at
                    break
        self._provider_bootstrap_progress[stage] = (current, last_progress)
        return last_progress

    def _record_nonconvergence(self, stage: str, target: int, reason: str) -> int:
        previous_target, previous_count, previous_reason = (
            self._nonconvergence_candidates.get(stage, (-1, 0, ""))
        )
        count = (
            previous_count + 1
            if previous_target == target and previous_reason == reason
            else 1
        )
        self._nonconvergence_candidates[stage] = (target, count, reason)
        return count

    def _record_downscale_candidate(self, stage: str, target: int) -> int:
        previous_target, previous_count = self._downscale_candidates.get(stage, (-1, 0))
        count = previous_count + 1 if previous_target == target else 1
        self._downscale_candidates[stage] = (target, count)
        return count

    def _prepare_drained_downscale(
        self,
        *,
        stage: str,
        rows: list[dict[str, object]],
        remove_count: int,
        current_replicas: int,
        expected_runtime_seconds: float,
    ) -> tuple[bool, str]:
        if remove_count <= 0:
            return True, "no_downscale_required"

        running_rows = [
            row for row in rows if str(row.get("status") or "").strip() == "running"
        ]
        running_worker_ids = {
            str(row.get("worker_id") or "").strip()
            for row in running_rows
            if str(row.get("worker_id") or "").strip()
        }
        try:
            instances = self.clients[stage].list_container_group_instances()
            matched_worker_ids: set[str] = set()
            active_instance_ids: set[str] = set()
            instance_by_id: dict[str, dict[str, object]] = {}
            running_rows_by_instance: dict[str, list[dict[str, object]]] = {}
            for instance in instances:
                instance_id = str(instance.get("id") or "").strip()
                if not instance_id:
                    continue
                instance_by_id[instance_id] = instance
                matched_rows = [
                    row
                    for row in running_rows
                    if instance_id in str(row.get("worker_id") or "").strip()
                ]
                if matched_rows:
                    active_instance_ids.add(instance_id)
                    running_rows_by_instance[instance_id] = matched_rows
                    matched_worker_ids.update(
                        str(row.get("worker_id") or "").strip() for row in matched_rows
                    )

            if matched_worker_ids != running_worker_ids:
                return False, "active_instance_mapping_incomplete"
            if len(instance_by_id) < current_replicas:
                return False, "instance_inventory_incomplete"

            existing_drains = self.store.list_draining_instances(stage=stage)
            observed_at = datetime.now(UTC)

            def candidate_key(instance_id: str) -> tuple[object, ...]:
                active = instance_id in active_instance_ids
                remaining = (
                    sum(
                        _remaining_running_seconds(
                            row=row,
                            expected_runtime_seconds=expected_runtime_seconds,
                            now=observed_at,
                        )
                        for row in running_rows_by_instance.get(instance_id, [])
                    )
                    if active
                    else 0.0
                )
                return (
                    instance_id not in existing_drains,
                    active,
                    remaining,
                    _optional_int(instance_by_id[instance_id].get("deletion_cost")) or 0,
                    instance_id,
                )

            candidate_ids = sorted(instance_by_id, key=candidate_key)
            if len(candidate_ids) < remove_count:
                return False, "instance_inventory_incomplete"
            draining_ids = tuple(candidate_ids[:remove_count])

            # Publishing a drain fences the selected worker from claiming another
            # shared-queue job. If it is currently active, it finishes that clip;
            # a later reconciliation observes it idle before requesting Salad resize.
            self.store.mark_draining_instances(
                stage=stage,
                instance_ids=draining_ids,
                ttl_seconds=self.config.drain_ttl_seconds,
            )
            self.store.clear_draining_instances(
                stage=stage,
                keep_instance_ids=draining_ids,
            )

            for instance_id, instance in instance_by_id.items():
                desired_cost = (
                    self.config.idle_deletion_cost
                    if instance_id in draining_ids
                    else self.config.active_deletion_cost
                )
                current_cost = _optional_int(instance.get("deletion_cost"))
                if current_cost != desired_cost:
                    self.clients[stage].set_container_group_instance_deletion_cost(
                        instance_id,
                        desired_cost,
                    )

            if any(instance_id in active_instance_ids for instance_id in draining_ids):
                return False, "draining_active_instances"

            drain_ready = all(
                instance_id in existing_drains
                and (
                    observed_at - existing_drains[instance_id]
                ).total_seconds()
                >= self.config.drain_grace_seconds
                for instance_id in draining_ids
            )
            if not drain_ready:
                return False, "drain_grace_pending"
        except Exception as error:
            self.logger(
                f"Autoscaler {stage}: could not establish a safe drain; "
                f"downscale deferred ({error})."
            )
            return False, "drain_protection_failed"
        return True, "drained_instances_ready"


    def _log_result(self, result: AutoscaleResult) -> None:
        demand = result.demand
        snapshot = (
            result.current_replicas,
            result.target_replicas,
            result.applied_replicas,
            tuple(sorted(demand.status_counts.items())),
            tuple(sorted(demand.run_counts.items())),
            round(demand.expected_runtime_seconds, 1),
            demand.jobs_per_replica,
            demand.job_count_target,
            result.reason,
        )
        if self._last_log_snapshot.get(result.stage) == snapshot:
            return
        self._last_log_snapshot[result.stage] = snapshot
        statuses = (
            ", ".join(
                f"{status}={count}" for status, count in sorted(demand.status_counts.items())
            )
            or "empty"
        )
        self.logger(
            f"Autoscaler {result.stage}: replicas "
            f"{result.current_replicas}->{result.applied_replicas} "
            f"(target={result.target_replicas}), jobs [{statuses}], "
            f"runs={len(demand.run_counts)}, runtime_p="
            f"{demand.expected_runtime_seconds:.1f}s, remaining_work="
            f"{demand.remaining_work_seconds:.1f}s"
            + (
                f", jobs_per_replica={demand.jobs_per_replica}, "
                f"job_target={demand.job_count_target}"
                if demand.jobs_per_replica is not None
                else ""
            )
            + f", reason={result.reason}."
        )


def build_global_demands(
    *,
    rows_by_stage: dict[str, list[dict[str, object]]],
    runtime_by_stage: dict[str, float],
    config: PredictiveAutoscalerConfig,
    now: datetime | None = None,
) -> dict[str, StageDemand]:
    observed_at = now or datetime.now(UTC)
    demands: dict[str, StageDemand] = {}
    jobs_per_replica_by_stage = config.stage_jobs_per_replica or {}
    for stage, rows in rows_by_stage.items():
        cold_start_seconds = config.cold_start_seconds
        if config.stage_cold_start_seconds is not None:
            cold_start_seconds = config.stage_cold_start_seconds.get(
                stage,
                cold_start_seconds,
            )
        available_drain_seconds = max(
            config.target_drain_seconds - cold_start_seconds,
            60.0,
        )
        expected_runtime = max(float(runtime_by_stage[stage]), 1.0)
        status_counts = Counter(str(row.get("status") or "unknown") for row in rows)
        run_counts = Counter(str(row.get("run_id") or "<unknown>") for row in rows)
        queued_count = (
            status_counts.get("pending", 0) + status_counts.get("retryable_failed", 0)
        )
        running_rows = [row for row in rows if str(row.get("status") or "") == "running"]
        running_work = sum(
            _remaining_running_seconds(
                row=row,
                expected_runtime_seconds=expected_runtime,
                now=observed_at,
            )
            for row in running_rows
        )
        forecast_blocked = 0
        work_seconds = queued_count * expected_runtime + running_work
        active_job_count = queued_count + len(running_rows)
        jobs_per_replica = jobs_per_replica_by_stage.get(stage)
        job_count_target: int | None = None
        if jobs_per_replica is not None:
            job_count_target = (
                math.ceil(active_job_count / jobs_per_replica)
                if active_job_count > 0
                else 0
            )
            # Clip-count targets may intentionally fall below the number of
            # currently running workers. Safe active draining prevents those
            # workers from claiming another clip and waits for them to finish
            # before the provider resize is issued.
            ideal = min(
                job_count_target,
                active_job_count,
                config.stage_max_replicas[stage],
            )
        else:
            ideal = (
                max(
                    len(running_rows),
                    math.ceil(
                        work_seconds
                        / (available_drain_seconds * config.target_utilization)
                    ),
                )
                if work_seconds > 0
                else 0
            )
            ideal = max(
                len(running_rows),
                min(ideal, active_job_count, config.stage_max_replicas[stage]),
            )
        demands[stage] = StageDemand(
            stage=stage,
            rows=tuple(dict(row) for row in rows),
            status_counts=dict(status_counts),
            run_counts=dict(run_counts),
            expected_runtime_seconds=expected_runtime,
            remaining_work_seconds=work_seconds,
            running_count=len(running_rows),
            forecast_blocked_count=forecast_blocked,
            ideal_replicas=ideal,
            jobs_per_replica=jobs_per_replica,
            job_count_target=job_count_target,
        )
    return demands


def allocate_project_quota(
    *,
    demands: dict[str, StageDemand],
    project_max_replicas: int,
) -> dict[str, int]:
    allocation = {
        stage: min(demand.running_count, demand.ideal_replicas)
        for stage, demand in demands.items()
    }
    capacity = max(project_max_replicas - sum(allocation.values()), 0)
    while capacity > 0:
        candidates = [
            stage
            for stage, demand in demands.items()
            if allocation[stage] < demand.ideal_replicas
        ]
        if not candidates:
            break
        stage = max(
            candidates,
            key=lambda candidate: (
                demands[candidate].remaining_work_seconds / (allocation[candidate] + 1),
                -tuple(demands).index(candidate),
            ),
        )
        allocation[stage] += 1
        capacity -= 1
    return allocation


def _remaining_running_seconds(
    *,
    row: dict[str, object],
    expected_runtime_seconds: float,
    now: datetime,
) -> float:
    started_at = _parse_datetime(row.get("started_at"))
    if started_at is None:
        return expected_runtime_seconds
    elapsed = max((now - started_at).total_seconds(), 0.0)
    return max(expected_runtime_seconds - elapsed, expected_runtime_seconds * 0.1)


def _runtime_percentile(
    values: list[float],
    *,
    percentile: float,
    fallback: float,
) -> float:
    ordered = sorted(float(value) for value in values if float(value) > 0)
    if not ordered:
        return float(fallback)
    index = max(math.ceil(percentile * len(ordered)) - 1, 0)
    return ordered[min(index, len(ordered) - 1)]


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _group_status(group: dict[str, object]) -> str:
    current_state = group.get("current_state")
    if not isinstance(current_state, dict):
        return "unknown"
    return str(current_state.get("status") or "").strip().lower() or "unknown"


def _bool_env(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def _int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _float_env(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else float(raw)
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _bounded_float_env(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = _float_env(name, default, minimum=minimum)
    if value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}")
    return value
