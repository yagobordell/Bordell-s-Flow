from __future__ import annotations

import math
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .coordination import acquire_instance_drain_lock
from .salad import SaladClient

ACTIVE_STATUSES = (
    "pending",
    "retryable_failed",
    "running",
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
    drain_grace_seconds: float = 5.0
    drain_ttl_seconds: float = 120.0
    nonconvergence_failure_polls: int = 4


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


@dataclass(frozen=True, slots=True)
class AutoscaleResult:
    stage: str
    current_replicas: int
    target_replicas: int
    applied_replicas: int
    changed: bool
    demand: StageDemand
    reason: str


def publish_instance_drains(
    connection: object,
    *,
    stage: str,
    instance_ids: tuple[str, ...],
    ttl_seconds: float,
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
                expires_at
            ) VALUES (%s, %s, now(), now() + (%s * interval '1 second'))
            ON CONFLICT (service, instance_id) DO UPDATE
            SET expires_at = EXCLUDED.expires_at
            """,
            (stage, instance_id, ttl_seconds),
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
                  AND expires_at > now()
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
                          expires_at <= now()
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
    cold_start_seconds = _float_env(
        "SALAD_AUTOSCALER_COLD_START_SECONDS",
        180.0,
        minimum=0.0,
    )
    stage_max: dict[str, int] = {}
    fallback_runtime: dict[str, float] = {}
    stage_cold_start: dict[str, float] = {}
    for binding in stage_bindings:
        configured_max = _int_env(
            f"SALAD_AUTOSCALER_{binding.autoscaler_env_prefix}_MAX_REPLICAS",
            binding.max_replicas,
            minimum=0,
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
        logger: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.store = store
        self.clients = dict(clients)
        self.bindings = dict(bindings)
        self.logger = logger
        self._downscale_candidates: dict[str, tuple[int, int]] = {}
        self._nonconvergence_candidates: dict[str, tuple[int, int, str]] = {}
        self._last_log_snapshot: dict[str, tuple[object, ...]] = {}

    def reconcile(self) -> dict[str, AutoscaleResult]:
        if not self.config.enabled or not self.clients:
            return {}
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
        current: dict[str, int] = {}
        for stage in self.clients:
            if group_status[stage] == "stopped":
                current[stage] = 0
                continue
            configured = max(int(groups[stage].get("replicas") or 0), 0)
            if not group_pending_change[stage]:
                current[stage] = configured
                continue
            try:
                live_instances = len(self.clients[stage].list_container_group_instances())
            except Exception as exc:
                raise AutoscalerReconciliationError(
                    "Salad capacity reconciliation cannot safely account for a "
                    f"pending provider change on {stage}: {exc}"
                ) from exc
            current[stage] = max(configured, live_instances)
        initial_current = dict(current)
        results: dict[str, AutoscaleResult] = {}
        hard_failures: dict[str, str] = {}

        # Preserve Media Pipeline ordering: only confirmed capacity releases can fund new capacity.
        for stage in self.clients:
            target = targets[stage]
            if target >= current[stage]:
                self._downscale_candidates.pop(stage, None)
                self._nonconvergence_candidates.pop(stage, None)
                self.store.clear_draining_instances(stage=stage)
                continue
            if group_pending_change[stage]:
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
            stable_count = self._record_downscale_candidate(stage, target)
            if stable_count < self.config.downscale_stable_polls:
                continue
            applied_target = max(
                target,
                current[stage] - self.config.max_downscale_per_poll,
            )
            protected, reason = self._prepare_drained_downscale(
                stage=stage,
                rows=list(demands[stage].rows),
                remove_count=current[stage] - applied_target,
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
                    "insufficient_idle_instances",
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

    def _record_nonconvergence(self, stage: str, target: int, reason: str) -> int:
        previous_target, previous_count, _ = self._nonconvergence_candidates.get(
            stage,
            (-1, 0, ""),
        )
        count = previous_count + 1 if previous_target == target else 1
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
    ) -> tuple[bool, str]:
        if remove_count <= 0:
            return True, "no_downscale_required"

        running_worker_ids = {
            str(row.get("worker_id") or "").strip()
            for row in rows
            if str(row.get("status") or "").strip() == "running"
            and str(row.get("worker_id") or "").strip()
        }
        try:
            instances = self.clients[stage].list_container_group_instances()
            matched_worker_ids: set[str] = set()
            active_instance_ids: set[str] = set()
            instance_by_id: dict[str, dict[str, object]] = {}
            for instance in instances:
                instance_id = str(instance.get("id") or "").strip()
                if not instance_id:
                    continue
                instance_by_id[instance_id] = instance
                instance_worker_ids = {
                    worker_id for worker_id in running_worker_ids if instance_id in worker_id
                }
                if instance_worker_ids:
                    active_instance_ids.add(instance_id)
                    matched_worker_ids.update(instance_worker_ids)

            if matched_worker_ids != running_worker_ids:
                return False, "active_instance_mapping_incomplete"

            idle_instance_ids = [
                instance_id
                for instance_id in instance_by_id
                if instance_id not in active_instance_ids
            ]
            if len(idle_instance_ids) < remove_count:
                return False, "insufficient_idle_instances"

            existing_drains = self.store.list_draining_instances(stage=stage)
            idle_instance_ids.sort(
                key=lambda instance_id: (
                    instance_id not in existing_drains,
                    _optional_int(instance_by_id[instance_id].get("deletion_cost"))
                    or 0,
                    instance_id,
                )
            )
            draining_ids = tuple(idle_instance_ids[:remove_count])
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

            observed_at = datetime.now(UTC)
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
            f"{demand.expected_runtime_seconds:.1f}s."
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
        ideal = (
            max(
                len(running_rows),
                math.ceil(
                    work_seconds / (available_drain_seconds * config.target_utilization)
                ),
            )
            if work_seconds > 0
            else 0
        )
        ideal = max(
            len(running_rows),
            min(ideal, config.stage_max_replicas[stage]),
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
