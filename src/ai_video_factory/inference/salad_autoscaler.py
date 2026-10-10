from __future__ import annotations

import math
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from .coordination import acquire_instance_drain_lock
from .ltx_economics import plan_ltx_capacity
from .ltx_performance_health import assess_ltx_instance_health
from .ltx_runtime_prediction import (
    degraded_instance_ids,
    forecast_ltx_job_seconds,
    typical_forecast_seconds,
)
from .salad import SaladClient

ACTIVE_STATUSES = (
    "pending",
    "retryable_failed",
    "running",
)

# LTX capacity is driven by aggregate GPU-work remaining in each shared task pool.
# The one-hour defaults reflect the production wall-clock objective for large LTX
# workloads while remaining configurable per stage.
_TIME_BASED_LTX_STAGES = frozenset({"ltx25_i2v", "ltx25"})
_DEFAULT_STAGE_TARGET_DRAIN_MINUTES = {
    "ltx25_i2v": 60.0,
    "ltx25": 60.0,
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
    stage_target_drain_seconds: dict[str, float] | None = None
    downscale_headroom_ratio: float = 0.90
    zero_demand_stable_polls: int = 1
    drain_grace_seconds: float = 5.0
    drain_ttl_seconds: float = 120.0
    nonconvergence_failure_polls: int = 4
    provider_pending_max_seconds: float = 7200.0
    ltx_cost_aware: bool = False
    ltx_gpu_hourly_usd: float = 0.50
    ltx_projected_pool_budget_usd: float | None = None
    ltx_health_mode: str = "observe"


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
    target_drain_seconds: float = 0.0
    effective_drain_seconds: float = 0.0
    estimated_drain_seconds: float = 0.0


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
                        run_id,
                        request ->> 'run_id',
                        request #>> '{parameters,run_id}',
                        '<unknown>'
                    ) AS run_id,
                    status,
                    lease_owner AS worker_id,
                    started_at,
                    request #>> '{parameters,generation_profile}' AS generation_profile,
                    request #>> '{parameters,width}' AS width,
                    request #>> '{parameters,height}' AS height,
                    request #>> '{parameters,num_frames}' AS num_frames
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
        self, *, stage: str, keep_instance_ids: tuple[str, ...] = (),
    ) -> None:
        # Normal capacity downscale cleanup cannot discard remediation fences.
        with self._pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM gpu.capacity_drains d
                WHERE d.service = %s
                  AND (
                      cardinality(%s::text[]) = 0
                      OR (d.expires_at <= now() AND NOT d.hold_until_confirmed)
                      OR NOT (d.instance_id = ANY(%s))
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM gpu.ltx_instance_reallocations r
                      WHERE r.stage = d.service AND r.instance_id = d.instance_id
                        AND r.state IN ('draining', 'requested', 'manual_review')
                  )
                """, (stage, list(keep_instance_ids), list(keep_instance_ids)),
            )

    def list_ltx_reallocation_intents(
        self, *, stage: str,
    ) -> dict[str, dict[str, object]]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT instance_id, original_machine_id, state, detected_at, requested_at
                FROM gpu.ltx_instance_reallocations WHERE stage = %s
                """, (stage,),
            ).fetchall()
        return {str(row["instance_id"]): dict(row) for row in rows}

    def begin_ltx_reallocation_drain(
        self, *, stage: str, instance_id: str, ttl_seconds: float,
        original_machine_id: str | None = None,
    ) -> bool:
        """Atomically fence future claims and persist exactly one drain intent."""
        with self._pool.connection() as connection, connection.transaction():
            acquire_instance_drain_lock(connection, instance_id)
            cursor = connection.execute(
                """
                INSERT INTO gpu.ltx_instance_reallocations(
                    stage, instance_id, original_machine_id, state
                ) VALUES (%s, %s, %s, 'draining') ON CONFLICT DO NOTHING
                """, (stage, instance_id, original_machine_id),
            )
            if cursor.rowcount != 1:
                return False
            publish_instance_drains(
                connection, stage=stage, instance_ids=(instance_id,),
                ttl_seconds=ttl_seconds, hold_until_confirmed=True,
            )
        return True

    def request_ltx_reallocation(
        self, *, stage: str, instance_id: str,
    ) -> bool:
        """Persist provider intent before dispatch; no automatic retries."""
        with self._pool.connection() as connection, connection.transaction():
            acquire_instance_drain_lock(connection, instance_id)
            updated = connection.execute(
                """
                UPDATE gpu.ltx_instance_reallocations
                SET state = 'requested', requested_at = now(), updated_at = now()
                WHERE stage = %s AND instance_id = %s AND state = 'draining'
                """, (stage, instance_id),
            )
            return updated.rowcount == 1

    def complete_ltx_reallocation(
        self, *, stage: str, instance_id: str,
    ) -> None:
        with self._pool.connection() as connection, connection.transaction():
            acquire_instance_drain_lock(connection, instance_id)
            connection.execute(
                """
                UPDATE gpu.ltx_instance_reallocations
                SET state = 'recovered', updated_at = now()
                WHERE stage = %s AND instance_id = %s
                  AND state IN ('requested', 'draining')
                """, (stage, instance_id),
            )
            connection.execute(
                """
                DELETE FROM gpu.capacity_drains
                WHERE service = %s AND instance_id = %s
                """, (stage, instance_id),
            )

    def flag_stalled_ltx_reallocation(
        self, *, stage: str, instance_id: str,
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE gpu.ltx_instance_reallocations
                SET state = 'manual_review', updated_at = now()
                WHERE stage = %s AND instance_id = %s AND state = 'requested'
                """, (stage, instance_id),
            )

    def list_recent_ltx_health_observations(
        self, *, stage: str, limit: int = 1500,
    ) -> list[dict[str, object]]:
        """Use immutable, completed and recent per-attempt telemetry only."""
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT job_id, attempt_count, recorded_at, instance_id,
                       instance_identity_source, stage, profile, width, height,
                       num_frames, inference_seconds, pipeline_reused,
                       task_seconds, pipeline_load_seconds
                FROM gpu.ltx_job_telemetry
                WHERE stage = %s AND recorded_at >= now() - interval '7 days'
                ORDER BY recorded_at DESC
                LIMIT %s
                """,
                (stage, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_ltx_instance_health(self, assessment: dict[str, object],
                                 *, recommendation: str) -> None:
        """Idempotent upsert; skips unchanged assessments to limit DB writes."""
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO gpu.ltx_instance_health (
                    stage, instance_id, health_state, ratio_to_exact_peers,
                    completed_jobs, matched_jobs, reference_instances,
                    recommendation
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stage, instance_id) DO UPDATE SET
                    health_state = EXCLUDED.health_state,
                    ratio_to_exact_peers = EXCLUDED.ratio_to_exact_peers,
                    completed_jobs = EXCLUDED.completed_jobs,
                    matched_jobs = EXCLUDED.matched_jobs,
                    reference_instances = EXCLUDED.reference_instances,
                    recommendation = EXCLUDED.recommendation,
                    updated_at = now()
                WHERE (
                    gpu.ltx_instance_health.health_state,
                    gpu.ltx_instance_health.ratio_to_exact_peers,
                    gpu.ltx_instance_health.completed_jobs,
                    gpu.ltx_instance_health.matched_jobs,
                    gpu.ltx_instance_health.reference_instances,
                    gpu.ltx_instance_health.recommendation
                ) IS DISTINCT FROM (
                    EXCLUDED.health_state, EXCLUDED.ratio_to_exact_peers,
                    EXCLUDED.completed_jobs, EXCLUDED.matched_jobs,
                    EXCLUDED.reference_instances, EXCLUDED.recommendation
                )
                """,
                (
                    assessment["stage"], assessment["instance_id"],
                    assessment["state"], assessment["ratio_to_exact_peers"],
                    assessment["completed_jobs"], assessment["exact_matched_jobs"],
                    assessment["peer_instances"], recommendation,
                ),
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
    global_target_drain_minutes = _float_env(
        "SALAD_AUTOSCALER_TARGET_DRAIN_MINUTES",
        20.0,
        minimum=1.0,
    )
    stage_max: dict[str, int] = {}
    fallback_runtime: dict[str, float] = {}
    stage_cold_start: dict[str, float] = {}
    stage_jobs_per_replica: dict[str, int] = {}
    stage_target_drain: dict[str, float] = {}
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
        default_target_minutes = _DEFAULT_STAGE_TARGET_DRAIN_MINUTES.get(
            binding.workload,
            global_target_drain_minutes,
        )
        stage_target_drain[binding.workload] = _float_env(
            f"SALAD_AUTOSCALER_{binding.autoscaler_env_prefix}_TARGET_DRAIN_MINUTES",
            default_target_minutes,
            minimum=1.0,
        ) * 60.0
    return PredictiveAutoscalerConfig(
        enabled=_bool_env("SALAD_AUTOSCALER_ENABLED", default=False),
        project_max_replicas=project_max,
        target_drain_seconds=global_target_drain_minutes * 60.0,
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
        stage_target_drain_seconds=stage_target_drain,
        downscale_headroom_ratio=_bounded_float_env(
            "SALAD_AUTOSCALER_DOWNSCALE_HEADROOM_RATIO",
            0.90,
            minimum=0.5,
            maximum=1.0,
        ),
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
        ltx_cost_aware=_bool_env(
            "SALAD_AUTOSCALER_LTX_COST_AWARE", default=False,
        ),
        ltx_gpu_hourly_usd=_float_env(
            "SALAD_AUTOSCALER_LTX_GPU_HOURLY_USD", 0.50, minimum=0.000001,
        ),
        ltx_projected_pool_budget_usd=(
            _float_env(
                "SALAD_AUTOSCALER_LTX_PROJECTED_POOL_BUDGET_USD",
                0.0, minimum=0.0,
            )
            if os.getenv("SALAD_AUTOSCALER_LTX_PROJECTED_POOL_BUDGET_USD", "").strip()
            else None
        ),
        ltx_health_mode=_ltx_health_mode_env(),
    )


def _ltx_health_mode_env() -> str:
    mode = os.getenv("SALAD_AUTOSCALER_LTX_HEALTH_MODE", "observe").strip().lower()
    if mode not in {"off", "observe", "recommend", "replace"}:
        raise RuntimeError(
            "SALAD_AUTOSCALER_LTX_HEALTH_MODE must be off, observe, recommend or replace"
        )
    return mode


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
        allowed_external_group_names: set[str] | None = None,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.store = store
        self.clients = dict(clients)
        self.bindings = dict(bindings)
        self.managed_group_names = (
            frozenset(managed_group_names) if managed_group_names is not None else None
        )
        self.allowed_external_group_names = frozenset(
            allowed_external_group_names or ()
        )
        self.logger = logger
        self._downscale_candidates: dict[str, tuple[int, int]] = {}
        self._nonconvergence_candidates: dict[str, tuple[int, int, str]] = {}
        self._provider_transition_first_seen: dict[str, datetime] = {}
        self._provider_bootstrap_progress: dict[
            str, tuple[dict[str, tuple[int | None, int, bool, float | None]], datetime]
        ] = {}
        self._last_log_snapshot: dict[str, tuple[object, ...]] = {}

    def _forecast_ltx_demands(
        self,
        rows_by_stage: dict[str, list[dict[str, object]]],
        runtime_by_stage: dict[str, float],
    ) -> None:
        """Optional evidence-based forecasts; missing telemetry leaves old fallback."""
        for stage, rows in rows_by_stage.items():
            if stage not in _TIME_BASED_LTX_STAGES or not rows:
                continue
            try:
                observations = self.store.list_recent_ltx_health_observations(
                    stage=stage
                )
                excluded = degraded_instance_ids(observations)
                for row in rows:
                    row["forecast_runtime_seconds"] = forecast_ltx_job_seconds(
                        row, observations, stage=stage,
                        fallback=runtime_by_stage[stage],
                        percentile=self.config.runtime_percentile,
                        excluded_instances=excluded,
                    )
                runtime_by_stage[stage] = typical_forecast_seconds(
                    rows, runtime_by_stage[stage]
                )
                if excluded:
                    self.logger(
                        f"Autoscaler {stage}: excluded {len(excluded)} confirmed "
                        "slow instance(s) from warm-job ETA history."
                    )
            except Exception as exc:
                self.logger(
                    f"Autoscaler {stage}: per-job forecasts unavailable, "
                    f"using conservative stage fallback ({exc})."
                )

    def _observe_ltx_health(self) -> None:
        """Observe only: no replica, draining or job mutations from assessments."""
        if self.config.ltx_health_mode == "off":
            return
        for stage in self.clients:
            if stage not in _TIME_BASED_LTX_STAGES:
                continue
            try:
                observations = self.store.list_recent_ltx_health_observations(
                    stage=stage
                )
                assessed = assess_ltx_instance_health(observations)
                for assessment in assessed:
                    status = str(assessment["state"])
                    recommendation = (
                        "review_safe_drain" if (
                            self.config.ltx_health_mode in {"recommend", "replace"}
                            and status == "confirmed_degraded"
                        ) else "observe"
                    )
                    self.store.save_ltx_instance_health(
                        assessment, recommendation=recommendation
                    )
                    if status in {"suspected_slow", "confirmed_degraded"}:
                        self.logger(
                            f"Autoscaler health {stage}/{assessment['instance_id']}: "
                            f"{status}, exact_matched={assessment['exact_matched_jobs']}, "
                            f"ratio={assessment['ratio_to_exact_peers']}; "
                            f"action={recommendation} (no provider mutation)."
                        )
            except Exception as exc:
                # A diagnostic outage is not a reason to abort safe capacity control.
                self.logger(f"Autoscaler health {stage}: telemetry unavailable ({exc}).")

    def _reconcile_ltx_reallocations(
        self, *, rows_by_stage: dict[str, list[dict[str, object]]],
        groups: dict[str, dict[str, object]],
        transitioning: dict[str, bool],
    ) -> set[str]:
        """Opt-in targeted Salad reallocation; never interrupt a leased job.

        Only the existing singleton controller calls this method. Reallocation
        does not alter replica counts and cannot exceed the global GPU quota.
        A durable DB intent is written before the provider POST; uncertain
        responses require operator review, never an automatic retry.
        """
        if self.config.ltx_health_mode != "replace":
            return set()
        busy: set[str] = set()
        now = datetime.now(UTC)
        for stage in self.clients:
            if stage not in _TIME_BASED_LTX_STAGES:
                continue
            try:
                intents = self.store.list_ltx_reallocation_intents(stage=stage)
                active = {
                    iid: entry for iid, entry in intents.items()
                    if entry["state"] in {"draining", "requested", "manual_review"}
                }
                if active:
                    busy.add(stage)
                # Do not issue/confirm capacity mutations during a provider transition.
                if transitioning[stage] or _group_status(groups[stage]) != "running":
                    continue
                instances = self.clients[stage].list_container_group_instances()
                instance_by_id = {
                    str(instance["id"]): instance for instance in instances
                    if instance.get("id")
                }
                # Unknown live-worker-to-instance mappings fail closed.
                running = [
                    row for row in rows_by_stage.get(stage, [])
                    if str(row.get("status") or "") == "running"
                ]
                active_ids: set[str] = set()
                mappings_complete = True
                for row in running:
                    worker_id = str(row.get("worker_id") or "").strip()
                    matches = [
                        iid for iid in instance_by_id if worker_id and iid in worker_id
                    ]
                    if len(matches) != 1:
                        mappings_complete = False
                        break
                    active_ids.add(matches[0])
                if not mappings_complete:
                    if active:
                        self.logger(
                            f"Autoscaler {stage}: reallocation still fenced, "
                            "incomplete active-worker mapping."
                        )
                    continue
                if active:
                    # Do not combine normal resize with per-instance reallocation.
                    for iid, entry in active.items():
                        if entry["state"] == "manual_review":
                            continue
                        original_machine = str(
                            entry.get("original_machine_id") or ""
                        ).strip()
                        replacement_machine = str(
                            instance_by_id.get(iid, {}).get("machine_id") or ""
                        ).strip()
                        moved = (
                            bool(original_machine and replacement_machine)
                            and original_machine != replacement_machine
                        )
                        if iid not in instance_by_id or moved:
                            self.store.complete_ltx_reallocation(
                                stage=stage, instance_id=iid
                            )
                            self.logger(
                                f"Autoscaler {stage}/{iid}: original Salad instance "
                                "absent; reallocation fence released."
                            )
                            continue
                        if entry["state"] == "requested":
                            requested = _parse_datetime(entry.get("requested_at"))
                            if requested is not None and (
                                now - requested
                            ).total_seconds() >= 7200:
                                self.store.flag_stalled_ltx_reallocation(
                                    stage=stage, instance_id=iid
                                )
                                self.logger(
                                    f"Autoscaler {stage}/{iid}: reallocation unresolved "
                                    "after 7200s; manual review required, no retry."
                                )
                            continue
                        if iid in active_ids:
                            continue
                        detected = _parse_datetime(entry.get("detected_at"))
                        if detected is None or (
                            now - detected
                        ).total_seconds() < self.config.drain_grace_seconds:
                            continue
                        # Instance is fenced from fresh claims and not leased.
                        if self.store.request_ltx_reallocation(
                            stage=stage, instance_id=iid
                        ):
                            self.clients[stage].reallocate_container_group_instance(iid)
                            self.logger(
                                f"Autoscaler {stage}/{iid}: targeted reallocation "
                                "requested; awaiting provider confirmation."
                            )
                    continue
                # Never interfere with a pre-existing ordinary downscale drain.
                if self.store.list_draining_instances(stage=stage):
                    continue
                observations = self.store.list_recent_ltx_health_observations(
                    stage=stage
                )
                scored = assess_ltx_instance_health(observations)
                candidates = [
                    row for row in scored
                    if row["state"] == "confirmed_degraded"
                    and row["instance_id"] in instance_by_id
                    and row["instance_id"] not in active_ids
                    and row["instance_id"] not in intents
                ]
                # The provider may be bootstrapping or capacity-constrained:
                # only move stable running instances, never a deploying one.
                candidates = [
                    row for row in candidates
                    if str(instance_by_id[str(row["instance_id"])].get("state"))
                    .lower() == "running"
                ]
                if not candidates:
                    continue
                candidate = max(
                    candidates,
                    key=lambda row: float(row["ratio_to_exact_peers"] or 0),
                )
                iid = str(candidate["instance_id"])
                if self.store.begin_ltx_reallocation_drain(
                    stage=stage, instance_id=iid,
                    ttl_seconds=max(self.config.drain_ttl_seconds, 120.0),
                    original_machine_id=(
                        str(instance_by_id[iid]["machine_id"])
                        if instance_by_id[iid].get("machine_id") else None
                    ),
                ):
                    busy.add(stage)
                    self.logger(
                        f"Autoscaler {stage}/{iid}: degradation "
                        f"ratio={candidate['ratio_to_exact_peers']} verified; "
                        "draining without killing active jobs. Reallocation "
                        "requires a later idle confirmation."
                    )
            except Exception as exc:
                # Ambiguous provider or database failures never justify deletion.
                busy.add(stage)
                self.logger(
                    f"Autoscaler {stage}: targeted reallocation deferred ({exc})."
                )
        return busy

    def reconcile(self) -> dict[str, AutoscaleResult]:
        if not self.config.enabled or not self.clients:
            return {}
        inventory_by_name = self._read_project_inventory()
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
        self._observe_ltx_health()
        self._forecast_ltx_demands(rows_by_stage, runtime_by_stage)
        demands = build_global_demands(
            rows_by_stage=rows_by_stage,
            runtime_by_stage=runtime_by_stage,
            config=self.config,
        )
        targets = allocate_project_quota(
            demands=demands,
            project_max_replicas=self.config.project_max_replicas,
        )
        if inventory_by_name is None:
            groups = {
                stage: self.clients[stage].describe_container_group()
                for stage in self.clients
            }
        else:
            groups: dict[str, dict[str, object]] = {}
            for stage, client in self.clients.items():
                group_name = client.container_group_name
                if not group_name:
                    raise AutoscalerReconciliationError(
                        f"Salad managed group identity is missing for {stage}"
                    )
                group = inventory_by_name.get(group_name)
                if group is None:
                    raise AutoscalerReconciliationError(
                        f"Salad managed container group is missing: {group_name}"
                    )
                groups[stage] = dict(group)
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
        if self.config.ltx_cost_aware:
            adjusted_demands = dict(demands)
            for stage, demand in demands.items():
                if stage not in _TIME_BASED_LTX_STAGES or group_transitioning[stage]:
                    continue
                startup = (
                    (self.config.stage_cold_start_seconds or {}).get(
                        stage, self.config.cold_start_seconds,
                    )
                )
                decision = plan_ltx_capacity(
                    remaining_work_seconds=demand.remaining_work_seconds,
                    active_jobs=(
                        demand.status_counts.get("pending", 0)
                        + demand.status_counts.get("retryable_failed", 0)
                        + demand.running_count
                    ),
                    running_jobs=demand.running_count,
                    current_replicas=current[stage],
                    max_replicas=self.config.stage_max_replicas[stage],
                    bootstrap_seconds=startup,
                    target_seconds=demand.target_drain_seconds,
                    utilization=self.config.target_utilization,
                    hourly_usd=self.config.ltx_gpu_hourly_usd,
                    projected_pool_budget_usd=(
                        self.config.ltx_projected_pool_budget_usd
                    ),
                )
                choice = decision.selected
                adjusted_demands[stage] = replace(
                    demand,
                    ideal_replicas=choice.replicas,
                    estimated_drain_seconds=(
                        choice.eta_seconds
                        if choice.eta_seconds is not None
                        else demand.estimated_drain_seconds
                    ),
                )
                eta_label = (
                    f"{choice.eta_seconds:.0f}s"
                    if choice.eta_seconds is not None
                    else "not_scheduled"
                )
                self.logger(
                    f"Autoscaler {stage}: cost-aware projection "
                    f"replicas={choice.replicas}, warm={choice.warm_replicas}, "
                    f"cold={choice.cold_replicas}, eta={eta_label}, "
                    f"estimated_usd={choice.projected_usd:.4f} "
                    f"(not invoice), reason={decision.reason}."
                )
            demands = adjusted_demands
            targets = allocate_project_quota(
                demands=demands,
                project_max_replicas=self.config.project_max_replicas,
            )
        quota_targets = dict(targets)
        targets = apply_downscale_hysteresis(
            demands=demands,
            targets=targets,
            current_replicas=current,
            config=self.config,
        )
        hysteresis_holds = {
            stage: targets[stage] > quota_targets[stage]
            for stage in self.clients
        }
        reallocation_busy = self._reconcile_ltx_reallocations(
            rows_by_stage=rows_by_stage, groups=groups,
            transitioning=group_transitioning,
        )
        initial_current = dict(current)
        results: dict[str, AutoscaleResult] = {}
        hard_failures: dict[str, str] = {}

        # Preserve Media Pipeline ordering: only confirmed capacity releases can fund new capacity.
        for stage in self.clients:
            target = targets[stage]
            if stage in reallocation_busy:
                results[stage] = AutoscaleResult(
                    stage=stage, current_replicas=current[stage],
                    target_replicas=target, applied_replicas=current[stage],
                    changed=False, demand=demands[stage],
                    reason="instance_reallocation_pending",
                )
                continue
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
            if group_transitioning[stage] or stage in reallocation_busy:
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
                reason=(
                    "reconciled"
                    if current[stage] != previous
                    else (
                        "downscale_hysteresis_hold"
                        if hysteresis_holds[stage]
                        else "stable"
                    )
                ),
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

    def _read_project_inventory(self) -> dict[str, dict[str, object]] | None:
        if self.managed_group_names is None:
            return None
        inventory_client = next(iter(self.clients.values()))
        groups = inventory_client.list_project_container_groups()
        groups_by_name: dict[str, dict[str, object]] = {}
        for group in groups:
            group_name = str(group.get("name") or "").strip()
            if not group_name:
                continue
            if group_name in groups_by_name:
                raise AutoscalerReconciliationError(
                    f"Salad project inventory contains duplicate group name: {group_name}"
                )
            groups_by_name[group_name] = dict(group)

        remote_names = set(groups_by_name)
        unmanaged = sorted(
            remote_names
            - self.managed_group_names
            - self.allowed_external_group_names
        )
        if unmanaged:
            raise AutoscalerReconciliationError(
                "Salad project contains unmanaged container groups outside "
                "the managed services and explicit external-service allowlist: "
                f"{', '.join(unmanaged)}"
            )
        missing = sorted(self.managed_group_names - remote_names)
        if missing:
            raise AutoscalerReconciliationError(
                "Salad project is missing managed container groups: "
                f"{', '.join(missing)}"
            )
        return groups_by_name

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
            round(demand.remaining_work_seconds, 1),
            round(demand.target_drain_seconds, 1),
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
        current_eta = (
            demand.remaining_work_seconds / result.current_replicas
            if result.current_replicas > 0
            else None
        )
        target_eta = (
            demand.remaining_work_seconds / result.target_replicas
            if result.target_replicas > 0
            else None
        )
        current_eta_text = f"{current_eta:.1f}s" if current_eta is not None else "inf"
        target_eta_text = f"{target_eta:.1f}s" if target_eta is not None else "inf"
        self.logger(
            f"Autoscaler {result.stage}: replicas "
            f"{result.current_replicas}->{result.applied_replicas} "
            f"(target={result.target_replicas}), jobs [{statuses}], "
            f"runs={len(demand.run_counts)}, runtime_p="
            f"{demand.expected_runtime_seconds:.1f}s, remaining_work="
            f"{demand.remaining_work_seconds:.1f}s, "
            f"eta_current={current_eta_text}, eta_target={target_eta_text}, "
            f"target_drain={demand.target_drain_seconds / 60.0:.1f}m, "
            f"effective_drain={demand.effective_drain_seconds / 60.0:.1f}m"
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
    target_drain_by_stage = config.stage_target_drain_seconds or {}
    for stage, rows in rows_by_stage.items():
        target_drain_seconds = max(
            target_drain_by_stage.get(stage, config.target_drain_seconds),
            60.0,
        )
        cold_start_seconds = config.cold_start_seconds
        if config.stage_cold_start_seconds is not None:
            cold_start_seconds = config.stage_cold_start_seconds.get(
                stage,
                cold_start_seconds,
            )
        # LTX intentionally ignores model/bootstrap time for capacity policy in this
        # iteration. The target describes inference drain time once capacity exists.
        effective_drain_seconds = (
            target_drain_seconds
            if stage in _TIME_BASED_LTX_STAGES
            else max(target_drain_seconds - cold_start_seconds, 60.0)
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
                expected_runtime_seconds=_job_forecast_seconds(row, expected_runtime),
                now=observed_at,
            )
            for row in running_rows
        )
        forecast_blocked = 0
        work_seconds = sum(
            _job_forecast_seconds(row, expected_runtime)
            for row in rows
            if str(row.get("status") or "") in {"pending", "retryable_failed"}
        ) + running_work
        active_job_count = queued_count + len(running_rows)
        jobs_per_replica = (
            None
            if stage in _TIME_BASED_LTX_STAGES
            else jobs_per_replica_by_stage.get(stage)
        )
        job_count_target: int | None = None
        if jobs_per_replica is not None:
            job_count_target = (
                math.ceil(active_job_count / jobs_per_replica)
                if active_job_count > 0
                else 0
            )
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
                        / (effective_drain_seconds * config.target_utilization)
                    ),
                )
                if work_seconds > 0
                else 0
            )
            ideal = max(
                len(running_rows),
                min(ideal, active_job_count, config.stage_max_replicas[stage]),
            )
        estimated_drain_seconds = work_seconds / ideal if ideal > 0 else 0.0
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
            target_drain_seconds=target_drain_seconds,
            effective_drain_seconds=effective_drain_seconds,
            estimated_drain_seconds=estimated_drain_seconds,
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
                demands[candidate].expected_runtime_seconds,
                candidate,
            ),
        )
        allocation[stage] += 1
        capacity -= 1
    return allocation


def apply_downscale_hysteresis(
    *,
    demands: Mapping[str, StageDemand],
    targets: Mapping[str, int],
    current_replicas: Mapping[str, int],
    config: PredictiveAutoscalerConfig,
) -> dict[str, int]:
    """Keep warm LTX capacity through a configurable ETA deadband before downscale."""

    adjusted = dict(targets)
    quota_constrained = any(
        adjusted[stage] < demand.ideal_replicas
        for stage, demand in demands.items()
    )
    if quota_constrained:
        # When the project cap is binding, retaining surplus capacity in one pool
        # would starve another pool whose ETA is already quota-constrained.
        return adjusted

    for stage, demand in demands.items():
        target = adjusted[stage]
        current = max(int(current_replicas.get(stage, 0)), 0)
        if (
            stage not in _TIME_BASED_LTX_STAGES
            or demand.remaining_work_seconds <= 0
            or target >= current
        ):
            continue
        downscale_window_seconds = (
            demand.effective_drain_seconds
            * config.target_utilization
            * config.downscale_headroom_ratio
        )
        if downscale_window_seconds <= 0:
            continue
        active_job_count = sum(demand.status_counts.values())
        hysteresis_floor = math.ceil(
            demand.remaining_work_seconds / downscale_window_seconds
        )
        hysteresis_floor = min(
            hysteresis_floor,
            active_job_count,
            config.stage_max_replicas[stage],
        )
        adjusted[stage] = min(current, max(target, hysteresis_floor))
    return adjusted


def _job_forecast_seconds(row: dict[str, object], fallback: float) -> float:
    try:
        value = float(row.get("forecast_runtime_seconds") or fallback)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return value if math.isfinite(value) and value > 0 else fallback


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
