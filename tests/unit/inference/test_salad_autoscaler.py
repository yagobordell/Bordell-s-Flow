from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ai_video_factory.inference.salad_autoscaler import (
    AutoscalerReconciliationError,
    AutoscalerServiceBinding,
    PredictiveAutoscalerConfig,
    PredictiveSaladAutoscaler,
    allocate_project_quota,
    apply_downscale_hysteresis,
    build_global_demands,
    load_predictive_autoscaler_config,
    next_reconcile_delay_seconds,
)


class FakeStore:
    def __init__(
        self,
        *,
        rows: dict[str, list[dict[str, object]]],
        runtimes: dict[str, list[float]],
    ) -> None:
        self.rows = rows
        self.runtimes = runtimes
        self.drains: dict[str, dict[str, datetime]] = {}
        self.held_drains: dict[str, set[str]] = {}

    def list_stage_jobs(self, *, binding, statuses):
        return [
            dict(row)
            for row in self.rows.get(binding.workload, [])
            if row.get("status") in statuses
        ]

    def list_recent_stage_runtime_seconds(self, *, binding, limit):
        return list(self.runtimes.get(binding.workload, []))[:limit]

    def list_draining_instances(self, *, stage: str) -> dict[str, datetime]:
        return dict(self.drains.get(stage, {}))

    def mark_draining_instances(
        self,
        *,
        stage: str,
        instance_ids: tuple[str, ...],
        ttl_seconds: float,
    ) -> None:
        del ttl_seconds
        current = self.drains.setdefault(stage, {})
        observed_at = datetime.now(UTC)
        held = self.held_drains.setdefault(stage, set())
        for instance_id in instance_ids:
            current.setdefault(instance_id, observed_at)
            held.discard(instance_id)

    def hold_draining_instances(
        self,
        *,
        stage: str,
        instance_ids: tuple[str, ...],
    ) -> None:
        current = self.drains.get(stage, {})
        missing = [instance_id for instance_id in instance_ids if instance_id not in current]
        if missing:
            raise RuntimeError(f"missing fake drain rows: {missing}")
        self.held_drains.setdefault(stage, set()).update(instance_ids)

    def expire_unheld_drains(self, *, stage: str) -> None:
        held = self.held_drains.get(stage, set())
        current = self.drains.get(stage, {})
        self.drains[stage] = {
            instance_id: requested_at
            for instance_id, requested_at in current.items()
            if instance_id in held
        }

    def clear_draining_instances(
        self,
        *,
        stage: str,
        keep_instance_ids: tuple[str, ...] = (),
    ) -> None:
        if not keep_instance_ids:
            self.drains.pop(stage, None)
            self.held_drains.pop(stage, None)
            return
        keep = set(keep_instance_ids)
        current = self.drains.setdefault(stage, {})
        self.drains[stage] = {
            instance_id: requested_at
            for instance_id, requested_at in current.items()
            if instance_id in keep
        }
        self.held_drains[stage] = self.held_drains.get(stage, set()) & keep


class FakeSaladClient:
    def __init__(
        self,
        *,
        replicas: int,
        status: str = "running",
        instances: list[dict[str, object]] | None = None,
        fail_deletion_cost: bool = False,
        pending_change: bool = False,
        stop_immediate: bool = True,
        replica_update_immediate: bool = True,
        fail_list_instances: bool = False,
        update_time: str | None = None,
        project_groups: list[dict[str, object]] | None = None,
        group_name: str | None = None,
        fail_describe: bool = False,
    ) -> None:
        self.replicas = replicas
        self.status = status
        self.instances = instances or []
        self.fail_deletion_cost = fail_deletion_cost
        self.pending_change = pending_change
        self.stop_immediate = stop_immediate
        self.replica_update_immediate = replica_update_immediate
        self.fail_list_instances = fail_list_instances
        self.update_time = update_time
        self.project_groups = project_groups or []
        self.container_group_name = str(group_name or "").strip()
        self.fail_describe = fail_describe
        self.describe_calls = 0
        self.replica_updates: list[int] = []
        self.deletion_cost_updates: list[tuple[str, int]] = []
        self.start_calls = 0
        self.stop_calls = 0

    def list_project_container_groups(self) -> list[dict[str, object]]:
        return [dict(item) for item in self.project_groups]

    def describe_container_group(self) -> dict[str, object]:
        self.describe_calls += 1
        if self.fail_describe:
            raise RuntimeError("unexpected per-group describe")
        return {
            "replicas": self.replicas,
            "pending_change": self.pending_change,
            "update_time": self.update_time,
            "current_state": {"status": self.status},
        }

    def set_container_group_replicas(self, replicas: int) -> dict[str, object]:
        self.replica_updates.append(replicas)
        self.replicas = replicas
        if not self.replica_update_immediate:
            self.pending_change = True
        return {"replicas": replicas}

    def start_container_group_if_needed(self, *, warning_logger=None) -> bool:
        self.start_calls += 1
        self.status = "deploying"
        return True

    def stop_container_group(self) -> None:
        self.stop_calls += 1
        if self.stop_immediate:
            self.status = "stopped"

    def list_container_group_instances(self) -> list[dict[str, object]]:
        if self.fail_list_instances:
            raise RuntimeError("Salad instance listing failed")
        return [dict(item) for item in self.instances]

    def set_container_group_instance_deletion_cost(
        self,
        instance_id: str,
        deletion_cost: int,
    ) -> dict[str, object]:
        if self.fail_deletion_cost:
            raise RuntimeError("Salad deletion_cost update failed")
        self.deletion_cost_updates.append((instance_id, deletion_cost))
        return {"id": instance_id, "deletion_cost": deletion_cost}


def _binding(stage: str, max_replicas: int = 4) -> AutoscalerServiceBinding:
    return AutoscalerServiceBinding(
        workload=stage,
        task_names=(f"test.{stage}",),
        autoscaler_env_prefix=stage.upper(),
        fallback_runtime_seconds=60.0,
        max_replicas=max_replicas,
    )


def _config(
    stages: tuple[str, ...],
    *,
    project_max_replicas: int = 30,
    stage_max_replicas: int = 4,
    target_drain_seconds: float = 600.0,
    target_utilization: float = 1.0,
    downscale_stable_polls: int = 1,
    zero_demand_stable_polls: int = 1,
    nonconvergence_failure_polls: int = 4,
    provider_pending_max_seconds: float = 7200.0,
    stage_cold_start_seconds: dict[str, float] | None = None,
    jobs_per_replica: dict[str, int] | None = None,
    stage_target_drain_seconds: dict[str, float] | None = None,
    downscale_headroom_ratio: float = 0.90,
) -> PredictiveAutoscalerConfig:
    return PredictiveAutoscalerConfig(
        enabled=True,
        project_max_replicas=project_max_replicas,
        target_drain_seconds=target_drain_seconds,
        cold_start_seconds=0.0,
        target_utilization=target_utilization,
        runtime_percentile=0.75,
        runtime_history_limit=200,
        downscale_stable_polls=downscale_stable_polls,
        zero_demand_stable_polls=zero_demand_stable_polls,
        nonconvergence_failure_polls=nonconvergence_failure_polls,
        max_upscale_per_poll=10,
        max_downscale_per_poll=10,
        active_deletion_cost=100_000,
        idle_deletion_cost=0,
        stage_max_replicas={stage: stage_max_replicas for stage in stages},
        fallback_runtime_seconds={stage: 60.0 for stage in stages},
        stage_cold_start_seconds=stage_cold_start_seconds,
        stage_jobs_per_replica=jobs_per_replica,
        stage_target_drain_seconds=stage_target_drain_seconds,
        downscale_headroom_ratio=downscale_headroom_ratio,
        drain_grace_seconds=0.0,
        drain_ttl_seconds=120.0,
        provider_pending_max_seconds=provider_pending_max_seconds,
    )


def _pending_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "job_id": f"job-{index}",
            "run_id": "run-a",
            "status": "pending",
            "worker_id": None,
            "started_at": None,
        }
        for index in range(count)
    ]


def test_unmanaged_project_group_fails_reconciliation_before_capacity_writes() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        group_name="ai-video-factory-ltx25-worker-v2",
        project_groups=[
            {"name": "ai-video-factory-ltx25-worker-v2"},
            {"name": "rogue-unmanaged-gpu-group"},
        ],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=1),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        managed_group_names={"ai-video-factory-ltx25-worker-v2"},
        logger=lambda _message: None,
    )

    with pytest.raises(
        AutoscalerReconciliationError,
        match="rogue-unmanaged-gpu-group",
    ):
        autoscaler.reconcile()

    assert client.replica_updates == []
    assert client.start_calls == 0
    assert client.stop_calls == 0


def test_explicit_external_project_group_is_allowed_but_not_managed() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        group_name="ai-video-factory-ltx25-worker-v2",
        fail_describe=True,
        project_groups=[
            {
                "name": "ai-video-factory-ltx25-worker-v2",
                "replicas": 1,
                "pending_change": False,
                "update_time": "2026-10-06T20:00:00+00:00",
                "version": 1,
                "current_state": {"status": "stopped"},
            },
            {"name": "bordell-image-upscaler-v2"},
        ],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=1),
        store=FakeStore(rows={stage: []}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        managed_group_names={"ai-video-factory-ltx25-worker-v2"},
        allowed_external_group_names={"bordell-image-upscaler-v2"},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.stage == stage
    assert client.replica_updates == []
    assert client.start_calls == 0


def test_project_inventory_snapshot_replaces_per_group_describe_reads() -> None:
    stages = ("whisper", "ltx25")
    group_names = {
        "whisper": "ai-video-factory-whisper-worker-v5",
        "ltx25": "ai-video-factory-ltx25-worker-v3",
    }
    inventory = [
        {
            "name": group_names[stage],
            "replicas": 1,
            "pending_change": False,
            "update_time": "2026-10-06T20:00:00+00:00",
            "version": 1,
            "current_state": {"status": "stopped"},
        }
        for stage in stages
    ]
    clients = {
        stage: FakeSaladClient(
            replicas=1,
            group_name=group_names[stage],
            project_groups=inventory if stage == "whisper" else [],
            fail_describe=True,
        )
        for stage in stages
    }
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(stages, project_max_replicas=2),
        store=FakeStore(
            rows={stage: [] for stage in stages},
            runtimes={stage: [60.0] for stage in stages},
        ),
        clients=clients,
        bindings={stage: _binding(stage, max_replicas=1) for stage in stages},
        managed_group_names=set(group_names.values()),
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()

    assert set(result) == set(stages)
    assert all(item.reason == "stable" for item in result.values())
    assert all(client.describe_calls == 0 for client in clients.values())


def test_project_quota_is_shared_across_global_stage_demands() -> None:
    stages = ("ltx25", "realesrgan")
    config = _config(stages, project_max_replicas=3, stage_max_replicas=4)
    demands = build_global_demands(
        rows_by_stage={
            "ltx25": _pending_rows(40),
            "realesrgan": _pending_rows(40),
        },
        runtime_by_stage={"ltx25": 60.0, "realesrgan": 60.0},
        config=config,
        now=datetime(2026, 9, 24, tzinfo=UTC),
    )

    allocation = allocate_project_quota(
        demands=demands,
        project_max_replicas=config.project_max_replicas,
    )

    assert sum(allocation.values()) == 3
    assert all(value <= demands[stage].ideal_replicas for stage, value in allocation.items())


@pytest.mark.parametrize(
    ("clip_count", "runtime_seconds", "expected_replicas"),
    [
        (0, 235.0, 0),
        (1, 235.0, 1),
        (13, 235.0, 1),
        (14, 235.0, 2),
        (52, 235.0, 4),
    ],
)
def test_ltx_capacity_scales_from_remaining_gpu_work(
    clip_count: int,
    runtime_seconds: float,
    expected_replicas: int,
) -> None:
    stage = "ltx25_i2v"
    config = _config(
        (stage,),
        stage_max_replicas=6,
        target_drain_seconds=3600.0,
        target_utilization=0.85,
        stage_target_drain_seconds={stage: 3600.0},
    )
    demand = build_global_demands(
        rows_by_stage={stage: _pending_rows(clip_count)},
        runtime_by_stage={stage: runtime_seconds},
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )[stage]

    assert demand.ideal_replicas == expected_replicas
    assert demand.jobs_per_replica is None
    assert demand.job_count_target is None
    assert demand.target_drain_seconds == 3600.0
    assert demand.effective_drain_seconds == 3600.0


def test_reference_test07_workload_requests_about_four_i2v_and_three_a2v() -> None:
    stages = ("ltx25_i2v", "ltx25")
    config = _config(
        stages,
        project_max_replicas=7,
        stage_max_replicas=6,
        target_drain_seconds=3600.0,
        target_utilization=0.85,
        stage_target_drain_seconds={stage: 3600.0 for stage in stages},
    )
    demands = build_global_demands(
        rows_by_stage={
            "ltx25_i2v": _pending_rows(52),
            "ltx25": _pending_rows(32),
        },
        runtime_by_stage={
            "ltx25_i2v": 235.0,
            "ltx25": 279.0,
        },
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )

    assert demands["ltx25_i2v"].remaining_work_seconds == pytest.approx(12_220.0)
    assert demands["ltx25"].remaining_work_seconds == pytest.approx(8_928.0)
    assert demands["ltx25_i2v"].ideal_replicas == 4
    assert demands["ltx25"].ideal_replicas == 3
    assert allocate_project_quota(
        demands=demands,
        project_max_replicas=config.project_max_replicas,
    ) == {"ltx25_i2v": 4, "ltx25": 3}


def test_ltx_29_a2v_jobs_do_not_collapse_two_gpus_to_one() -> None:
    stage = "ltx25"
    config = _config(
        (stage,),
        stage_max_replicas=4,
        target_drain_seconds=3600.0,
        target_utilization=0.85,
        stage_target_drain_seconds={stage: 3600.0},
    )
    demand = build_global_demands(
        rows_by_stage={stage: _pending_rows(29)},
        runtime_by_stage={stage: 279.0},
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )[stage]

    assert demand.remaining_work_seconds == pytest.approx(8_091.0)
    assert demand.ideal_replicas == 3


def test_ltx_downscale_hysteresis_requires_eta_headroom() -> None:
    stage = "ltx25"
    config = _config(
        (stage,),
        stage_max_replicas=4,
        target_drain_seconds=600.0,
        target_utilization=1.0,
        stage_target_drain_seconds={stage: 600.0},
        downscale_headroom_ratio=0.90,
    )
    near_boundary = build_global_demands(
        rows_by_stage={stage: _pending_rows(18)},
        runtime_by_stage={stage: 100.0},
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert near_boundary[stage].ideal_replicas == 3
    assert apply_downscale_hysteresis(
        demands=near_boundary,
        targets={stage: 3},
        current_replicas={stage: 4},
        config=config,
    )[stage] == 4

    safely_inside = build_global_demands(
        rows_by_stage={stage: _pending_rows(16)},
        runtime_by_stage={stage: 100.0},
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert safely_inside[stage].ideal_replicas == 3
    assert apply_downscale_hysteresis(
        demands=safely_inside,
        targets={stage: 3},
        current_replicas={stage: 4},
        config=config,
    )[stage] == 3


def test_project_cap_prioritizes_ltx_pools_by_remaining_eta() -> None:
    stages = ("ltx25_i2v", "ltx25")
    config = _config(
        stages,
        project_max_replicas=5,
        stage_max_replicas=6,
        target_drain_seconds=3600.0,
        target_utilization=0.85,
        stage_target_drain_seconds={stage: 3600.0 for stage in stages},
    )
    demands = build_global_demands(
        rows_by_stage={
            "ltx25_i2v": _pending_rows(52),
            "ltx25": _pending_rows(32),
        },
        runtime_by_stage={
            "ltx25_i2v": 235.0,
            "ltx25": 279.0,
        },
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )

    assert allocate_project_quota(
        demands=demands,
        project_max_replicas=5,
    ) == {"ltx25_i2v": 3, "ltx25": 2}


def test_ltx_running_workers_are_not_targeted_below_active_capacity() -> None:
    stage = "ltx25_i2v"
    now = datetime.now(UTC)
    rows = {
        stage: [
            {
                "job_id": "job-running-a",
                "run_id": "run-a",
                "status": "running",
                "worker_id": "worker-instance-a",
                "started_at": now,
            },
            {
                "job_id": "job-running-b",
                "run_id": "run-a",
                "status": "running",
                "worker_id": "worker-instance-b",
                "started_at": now,
            },
        ]
    }
    store = FakeStore(rows=rows, runtimes={stage: [60.0]})
    client = FakeSaladClient(
        replicas=2,
        instances=[
            {"id": "instance-a", "deletion_cost": 100_000},
            {"id": "instance-b", "deletion_cost": 100_000},
        ],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=store,
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    stable = autoscaler.reconcile()[stage]

    assert stable.target_replicas == 2
    assert stable.applied_replicas == 2
    assert stable.reason == "stable"
    assert client.replica_updates == []
    assert store.drains.get(stage) in (None, {})

    store.rows[stage] = [store.rows[stage][0]]
    draining = autoscaler.reconcile()[stage]
    assert draining.target_replicas == 1
    assert draining.reason == "drain_grace_pending"
    assert client.replica_updates == []

    requested = autoscaler.reconcile()[stage]
    assert requested.target_replicas == 1
    assert requested.reason == "resize_requested_pending_confirmation"
    assert client.replica_updates == [1]

def test_stopped_group_starts_without_rewriting_matching_configured_replicas() -> None:
    stage = "ltx25"
    client = FakeSaladClient(replicas=1, status="stopped")
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.applied_replicas == 1
    assert client.replica_updates == []
    assert client.start_calls == 1


def test_stopped_group_settles_replica_patch_before_start() -> None:
    stage = "ltx25"
    client = FakeSaladClient(replicas=4, status="stopped")
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    patched = autoscaler.reconcile()[stage]

    assert patched.applied_replicas == 0
    assert patched.reason == "resize_before_start_pending_confirmation"
    assert client.replica_updates == [1]
    assert client.start_calls == 0

    started = autoscaler.reconcile()[stage]

    assert started.applied_replicas == 1
    assert client.replica_updates == [1]
    assert client.start_calls == 1


def test_downscale_protects_running_instance_with_deletion_cost() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=2,
        instances=[
            {"id": "instance-active", "deletion_cost": 0},
            {"id": "instance-idle", "deletion_cost": 100_000},
        ],
    )
    rows = {
        stage: [
            {
                "job_id": "job-running",
                "run_id": "run-a",
                "status": "running",
                "worker_id": "worker-instance-active",
                "started_at": datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
            }
        ]
    }
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows=rows, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()[stage]
    requested = autoscaler.reconcile()[stage]
    confirmed = autoscaler.reconcile()[stage]

    assert first.applied_replicas == 2
    assert first.reason == "drain_grace_pending"
    assert requested.applied_replicas == 2
    assert requested.reason == "resize_requested_pending_confirmation"
    assert confirmed.applied_replicas == 1
    assert ("instance-active", 100_000) in client.deletion_cost_updates
    assert ("instance-idle", 0) in client.deletion_cost_updates
    assert client.replica_updates == [1]


def test_downscale_is_deferred_when_running_worker_cannot_map_to_instance() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=2,
        instances=[{"id": "different-instance", "deletion_cost": 0}],
    )
    rows = {
        stage: [
            {
                "job_id": "job-running",
                "run_id": "run-a",
                "status": "running",
                "worker_id": "worker-instance-active",
                "started_at": None,
            }
        ]
    }
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows=rows, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.applied_replicas == 2
    assert result.reason == "active_instance_mapping_incomplete"
    assert client.replica_updates == []


def test_pending_salad_change_defers_additional_zero_demand_writes() -> None:
    stage = "realesrgan"
    client = FakeSaladClient(
        replicas=1,
        status="pending",
        pending_change=True,
        instances=[],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows={stage: []}, runtimes={stage: [53.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.target_replicas == 0
    assert result.applied_replicas == 1
    assert result.reason == "provider_change_pending"
    assert client.replica_updates == []
    assert client.stop_calls == 0


def test_pending_provider_change_never_emits_reverse_upscale_write() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=2,
        pending_change=True,
        instances=[
            {"id": "instance-a", "deletion_cost": 100_000},
            {"id": "instance-b", "deletion_cost": 0},
        ],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=3, stage_max_replicas=3),
        store=FakeStore(rows={stage: _pending_rows(30)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=3)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.target_replicas == 3
    assert result.applied_replicas == 2
    assert result.reason == "provider_change_pending"
    assert client.replica_updates == []
    assert client.start_calls == 0


def test_stopped_pending_group_is_not_restarted_until_provider_settles() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        status="stopped",
        pending_change=True,
        instances=[],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=1, stage_max_replicas=1),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.target_replicas == 1
    assert result.applied_replicas == 1
    assert result.reason == "provider_change_pending"
    assert client.replica_updates == []
    assert client.start_calls == 0


def test_zero_demand_starts_drain_before_general_downscale_stability_window() -> None:
    stage = "ltx25_i2v"
    client = FakeSaladClient(
        replicas=1,
        instances=[{"id": "idle-instance", "deletion_cost": 0}],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            (stage,),
            downscale_stable_polls=3,
            zero_demand_stable_polls=1,
        ),
        store=FakeStore(rows={stage: []}, runtimes={stage: [206.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()[stage]

    assert first.target_replicas == 0
    assert first.reason == "drain_grace_pending"
    assert next_reconcile_delay_seconds(
        {stage: first},
        poll_seconds=15.0,
        drain_grace_seconds=5.0,
    ) == 5.0


def test_target_zero_with_pending_work_keeps_general_stability_window() -> None:
    stages = ("busy", "blocked")
    store = FakeStore(
        rows={
            "busy": _pending_rows(40),
            "blocked": _pending_rows(1),
        },
        runtimes={"busy": [60.0], "blocked": [60.0]},
    )
    clients = {
        "busy": FakeSaladClient(replicas=1, status="stopped"),
        "blocked": FakeSaladClient(
            replicas=1,
            instances=[{"id": "blocked-idle", "deletion_cost": 0}],
        ),
    }
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            stages,
            project_max_replicas=1,
            stage_max_replicas=1,
            downscale_stable_polls=3,
            zero_demand_stable_polls=1,
        ),
        store=store,
        clients=clients,
        bindings={stage: _binding(stage, max_replicas=1) for stage in stages},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()

    assert first["blocked"].target_replicas == 0
    assert first["blocked"].demand.remaining_work_seconds > 0
    assert first["blocked"].reason == "stable"
    assert store.drains.get("blocked") in (None, {})


def test_empty_global_queue_releases_capacity_only_after_stopped_is_observed() -> None:
    stage = "realesrgan"
    client = FakeSaladClient(
        replicas=1,
        instances=[{"id": "idle-instance", "deletion_cost": 0}],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows={stage: []}, runtimes={stage: [53.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()[stage]
    stop_requested = autoscaler.reconcile()[stage]
    stopped = autoscaler.reconcile()[stage]

    assert first.target_replicas == 0
    assert first.applied_replicas == 1
    assert first.reason == "drain_grace_pending"
    assert stop_requested.target_replicas == 0
    assert stop_requested.applied_replicas == 1
    assert stop_requested.reason == "stop_requested_pending_confirmation"
    assert stopped.applied_replicas == 0
    assert client.replica_updates == []
    assert client.stop_calls == 1
    assert client.replicas == 1
    assert client.status == "stopped"


def test_stop_request_does_not_release_project_quota_until_confirmed() -> None:
    stages = ("realesrgan", "ltx25")
    store = FakeStore(
        rows={
            "realesrgan": [],
            "ltx25": _pending_rows(2),
        },
        runtimes={
            "realesrgan": [53.0],
            "ltx25": [60.0],
        },
    )
    stopping = FakeSaladClient(
        replicas=1,
        status="running",
        instances=[{"id": "idle-instance", "deletion_cost": 0}],
        stop_immediate=False,
    )
    waiting = FakeSaladClient(replicas=1, status="stopped")
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(stages, project_max_replicas=1, stage_max_replicas=1),
        store=store,
        clients={
            "realesrgan": stopping,
            "ltx25": waiting,
        },
        bindings={stage: _binding(stage, max_replicas=1) for stage in stages},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()
    second = autoscaler.reconcile()

    assert first["realesrgan"].reason == "drain_grace_pending"
    assert second["realesrgan"].reason == "stop_requested_pending_confirmation"
    assert second["realesrgan"].applied_replicas == 1
    assert stopping.stop_calls == 1
    assert waiting.start_calls == 0
    assert waiting.replica_updates == []

    stopping.status = "stopped"
    confirmed = autoscaler.reconcile()

    assert confirmed["realesrgan"].applied_replicas == 0
    assert confirmed["ltx25"].applied_replicas == 1
    assert waiting.start_calls == 1


def test_partial_downscale_keeps_project_quota_reserved_until_provider_confirms() -> None:
    stages = ("ltx25", "realesrgan")
    store = FakeStore(
        rows={
            "ltx25": _pending_rows(2),
            "realesrgan": _pending_rows(2),
        },
        runtimes={
            "ltx25": [60.0],
            "realesrgan": [60.0],
        },
    )
    shrinking = FakeSaladClient(
        replicas=2,
        instances=[
            {"id": "instance-a", "deletion_cost": 0},
            {"id": "instance-b", "deletion_cost": 0},
        ],
        replica_update_immediate=False,
    )
    waiting = FakeSaladClient(replicas=1, status="stopped")
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(stages, project_max_replicas=2, stage_max_replicas=2),
        store=store,
        clients={
            "ltx25": shrinking,
            "realesrgan": waiting,
        },
        bindings={stage: _binding(stage, max_replicas=2) for stage in stages},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()
    requested = autoscaler.reconcile()

    assert first["ltx25"].reason == "drain_grace_pending"
    assert requested["ltx25"].reason == "resize_requested_pending_confirmation"
    assert requested["ltx25"].applied_replicas == 2
    assert shrinking.replica_updates == [1]
    assert shrinking.pending_change is True
    assert waiting.start_calls == 0

    pending = autoscaler.reconcile()

    assert pending["ltx25"].reason == "provider_change_pending"
    assert pending["ltx25"].applied_replicas == 2
    assert waiting.start_calls == 0

    shrinking.pending_change = False
    shrinking.instances = [{"id": "instance-a", "deletion_cost": 0}]
    confirmed = autoscaler.reconcile()

    assert confirmed["ltx25"].applied_replicas == 1
    assert confirmed["realesrgan"].applied_replicas == 1
    assert waiting.start_calls == 1


def test_pending_resize_keeps_drains_held_through_ttl_and_demand_rebound() -> None:
    stage = "ltx25"
    store = FakeStore(
        rows={stage: _pending_rows(2)},
        runtimes={stage: [60.0]},
    )
    client = FakeSaladClient(
        replicas=2,
        instances=[
            {"id": "instance-a", "deletion_cost": 0},
            {"id": "instance-b", "deletion_cost": 0},
        ],
        replica_update_immediate=False,
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=2, stage_max_replicas=2),
        store=store,
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=2)},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()[stage]
    requested = autoscaler.reconcile()[stage]

    assert first.reason == "drain_grace_pending"
    assert requested.reason == "resize_requested_pending_confirmation"
    assert store.drains[stage]
    assert store.held_drains[stage] == set(store.drains[stage])

    store.expire_unheld_drains(stage=stage)
    assert store.drains[stage]

    store.rows[stage] = _pending_rows(20)
    pending = autoscaler.reconcile()[stage]

    assert pending.reason == "provider_change_pending"
    assert store.drains[stage]
    assert store.held_drains[stage] == set(store.drains[stage])

    client.pending_change = False
    client.instances = [{"id": "instance-a", "deletion_cost": 0}]
    confirmed = autoscaler.reconcile()[stage]

    assert confirmed.target_replicas >= confirmed.current_replicas
    assert store.drains.get(stage) in (None, {})
    assert store.held_drains.get(stage) in (None, set())


def test_provider_pending_promotes_existing_drain_after_controller_restart() -> None:
    stage = "ltx25"
    store = FakeStore(
        rows={stage: _pending_rows(20)},
        runtimes={stage: [60.0]},
    )
    store.drains[stage] = {
        "instance-b": datetime(2026, 9, 25, tzinfo=UTC),
    }
    client = FakeSaladClient(
        replicas=1,
        pending_change=True,
        instances=[
            {"id": "instance-a", "deletion_cost": 100_000},
            {"id": "instance-b", "deletion_cost": 0},
        ],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=2, stage_max_replicas=2),
        store=store,
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=2)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.reason == "provider_change_pending"
    assert store.held_drains[stage] == {"instance-b"}
    store.expire_unheld_drains(stage=stage)
    assert set(store.drains[stage]) == {"instance-b"}


def test_pending_change_uses_live_instance_count_for_restart_safe_quota_accounting() -> None:
    stages = ("ltx25", "realesrgan")
    shrinking = FakeSaladClient(
        replicas=1,
        pending_change=True,
        instances=[
            {"id": "instance-a", "deletion_cost": 0},
            {"id": "instance-b", "deletion_cost": 0},
        ],
    )
    waiting = FakeSaladClient(replicas=1, status="stopped")
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(stages, project_max_replicas=2, stage_max_replicas=2),
        store=FakeStore(
            rows={
                "ltx25": _pending_rows(2),
                "realesrgan": _pending_rows(2),
            },
            runtimes={
                "ltx25": [60.0],
                "realesrgan": [60.0],
            },
        ),
        clients={
            "ltx25": shrinking,
            "realesrgan": waiting,
        },
        bindings={stage: _binding(stage, max_replicas=2) for stage in stages},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()

    assert result["ltx25"].applied_replicas == 2
    assert result["ltx25"].reason == "provider_change_pending"
    assert waiting.start_calls == 0


def test_stale_provider_pending_change_degrades_reconciliation() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        pending_change=True,
        instances=[{"id": "instance-a", "deletion_cost": 0}],
        update_time="2000-01-01T00:00:00+00:00",
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            (stage,),
            project_max_replicas=1,
            provider_pending_max_seconds=60.0,
        ),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )

    with pytest.raises(
        AutoscalerReconciliationError,
        match="has remained active",
    ):
        autoscaler.reconcile()

    assert client.replica_updates == []
    assert client.start_calls == 0
    assert client.stop_calls == 0



def test_old_deploying_group_remains_read_only_while_instance_progresses() -> None:
    stage = "ltx25_i2v"
    client = FakeSaladClient(
        replicas=1,
        status="deploying",
        pending_change=False,
        update_time="2000-01-01T00:00:00+00:00",
        instances=[{
            "id": "i2v-allocated", "version": 4,
            "state": "allocating", "started": False, "pulling_progress": None,
        }],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            (stage,), project_max_replicas=1, provider_pending_max_seconds=60.0,
        ),
        store=FakeStore(rows={stage: _pending_rows(1)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )

    assert autoscaler.reconcile()[stage].reason == "provider_change_pending"
    client.instances[0]["state"] = "downloading"
    client.instances[0]["pulling_progress"] = 0.46
    assert autoscaler.reconcile()[stage].reason == "provider_change_pending"

    # Progress must not grant unlimited grace to a genuinely stalled pull.
    snapshot, _last_progress = autoscaler._provider_bootstrap_progress[stage]
    autoscaler._provider_bootstrap_progress[stage] = (
        snapshot, datetime(2000, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(AutoscalerReconciliationError, match="has remained active"):
        autoscaler.reconcile()
    assert client.replica_updates == []
    assert client.start_calls == 0
    assert client.stop_calls == 0


def test_old_deploying_group_without_instances_still_fails_closed() -> None:
    stage = "ltx25_i2v"
    client = FakeSaladClient(
        replicas=1,
        status="deploying",
        pending_change=False,
        update_time="2000-01-01T00:00:00+00:00",
        instances=[],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            (stage,), project_max_replicas=1, provider_pending_max_seconds=60.0,
        ),
        store=FakeStore(rows={stage: _pending_rows(1)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )
    with pytest.raises(AutoscalerReconciliationError, match="has remained active"):
        autoscaler.reconcile()
    assert client.start_calls == 0



def test_pending_watchdog_fallback_starts_fresh_when_update_time_is_missing() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        pending_change=True,
        instances=[{"id": "instance-a", "deletion_cost": 0}],
        update_time=None,
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            (stage,),
            project_max_replicas=1,
            provider_pending_max_seconds=60.0,
        ),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.reason == "provider_change_pending"
    assert client.replica_updates == []


def test_pending_change_instance_accounting_failure_fails_closed() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        pending_change=True,
        fail_list_instances=True,
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=1),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )

    with pytest.raises(
        AutoscalerReconciliationError,
        match="cannot safely account for an active provider transition",
    ):
        autoscaler.reconcile()

    assert client.replica_updates == []
    assert client.start_calls == 0
    assert client.stop_calls == 0


@pytest.mark.parametrize("status", ["failed", "succeeded"])
def test_terminal_provider_state_degrades_reconciliation(status: str) -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        status=status,
        instances=[],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=1),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )

    with pytest.raises(
        AutoscalerReconciliationError,
        match="provider state.*terminal",
    ):
        autoscaler.reconcile()

    assert client.replica_updates == []
    assert client.start_calls == 0
    assert client.stop_calls == 0


def test_deploying_group_is_read_only_without_pending_change_flag() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        status="deploying",
        pending_change=False,
        instances=[{"id": "instance-a", "deletion_cost": 0}],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=2, stage_max_replicas=2),
        store=FakeStore(rows={stage: _pending_rows(30)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=2)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.reason == "provider_change_pending"
    assert result.applied_replicas == 1
    assert client.replica_updates == []
    assert client.start_calls == 0
    assert client.stop_calls == 0


def test_stale_deploying_group_degrades_without_pending_change_flag() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        status="deploying",
        pending_change=False,
        instances=[{"id": "instance-a", "deletion_cost": 0}],
        update_time="2000-01-01T00:00:00+00:00",
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            (stage,),
            project_max_replicas=1,
            provider_pending_max_seconds=60.0,
        ),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )

    with pytest.raises(
        AutoscalerReconciliationError,
        match="provider transition.*remained active",
    ):
        autoscaler.reconcile()

    assert client.replica_updates == []
    assert client.start_calls == 0
    assert client.stop_calls == 0


def test_unknown_provider_state_fails_closed() -> None:
    stage = "ltx25"
    client = FakeSaladClient(
        replicas=1,
        status="mystery",
        instances=[],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), project_max_replicas=1),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage, max_replicas=1)},
        logger=lambda _message: None,
    )

    with pytest.raises(
        AutoscalerReconciliationError,
        match="provider state.*unknown",
    ):
        autoscaler.reconcile()

    assert client.replica_updates == []
    assert client.start_calls == 0
    assert client.stop_calls == 0


def test_ltx_groups_have_independent_runtime_and_time_target_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SALAD_AUTOSCALER_LTX25_I2V_COLD_START_SECONDS", "3090")
    monkeypatch.setenv("SALAD_AUTOSCALER_LTX25_COLD_START_SECONDS", "1200")
    monkeypatch.setenv("SALAD_AUTOSCALER_LTX25_I2V_RUNTIME_SECONDS", "240")
    monkeypatch.setenv("SALAD_AUTOSCALER_LTX25_RUNTIME_SECONDS", "320")
    monkeypatch.setenv("SALAD_AUTOSCALER_LTX25_I2V_TARGET_DRAIN_MINUTES", "50")
    monkeypatch.setenv("SALAD_AUTOSCALER_LTX25_TARGET_DRAIN_MINUTES", "70")
    monkeypatch.setenv("SALAD_AUTOSCALER_ZERO_DEMAND_STABLE_POLLS", "1")

    config = load_predictive_autoscaler_config(
        stage_bindings=(
            _binding("ltx25_i2v"),
            _binding("ltx25"),
        )
    )

    assert config.stage_cold_start_seconds == {
        "ltx25_i2v": 3090.0,
        "ltx25": 1200.0,
    }
    assert config.fallback_runtime_seconds == {
        "ltx25_i2v": 240.0,
        "ltx25": 320.0,
    }
    assert config.stage_jobs_per_replica == {}
    assert config.stage_target_drain_seconds == {
        "ltx25_i2v": 3000.0,
        "ltx25": 4200.0,
    }
    assert config.downscale_headroom_ratio == pytest.approx(0.90)
    assert config.zero_demand_stable_polls == 1


def test_ltx_time_target_and_cost_ceiling_allow_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SALAD_AUTOSCALER_LTX25_TARGET_DRAIN_MINUTES", "45")
    monkeypatch.setenv("SALAD_AUTOSCALER_DOWNSCALE_HEADROOM_RATIO", "0.80")
    monkeypatch.setenv("SALAD_AUTOSCALER_LTX25_MAX_REPLICAS", "1")
    monkeypatch.setenv("SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS", "1")

    config = load_predictive_autoscaler_config(
        stage_bindings=(_binding("ltx25", max_replicas=2),)
    )

    assert config.project_max_replicas == 1
    assert config.stage_max_replicas["ltx25"] == 1
    assert config.stage_target_drain_seconds == {"ltx25": 2700.0}
    assert config.downscale_headroom_ratio == pytest.approx(0.80)
    assert config.stage_jobs_per_replica == {}


def test_ltx_defaults_use_one_hour_time_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "SALAD_AUTOSCALER_LTX25_I2V_TARGET_DRAIN_MINUTES",
        "SALAD_AUTOSCALER_LTX25_TARGET_DRAIN_MINUTES",
        "SALAD_AUTOSCALER_LTX25_I2V_JOBS_PER_REPLICA",
        "SALAD_AUTOSCALER_LTX25_JOBS_PER_REPLICA",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_predictive_autoscaler_config(
        stage_bindings=(
            _binding("ltx25_i2v", max_replicas=2),
            _binding("ltx25", max_replicas=2),
        )
    )

    assert config.stage_jobs_per_replica == {}
    assert config.stage_target_drain_seconds == {
        "ltx25_i2v": 3600.0,
        "ltx25": 3600.0,
    }

    for count, expected in ((2, 1), (13, 1), (14, 2), (100, 2)):
        demands = build_global_demands(
            rows_by_stage={
                "ltx25_i2v": _pending_rows(count),
                "ltx25": _pending_rows(count),
            },
            runtime_by_stage={"ltx25_i2v": 235.0, "ltx25": 235.0},
            config=config,
            now=datetime(2026, 10, 8, tzinfo=UTC),
        )
        assert demands["ltx25_i2v"].ideal_replicas == expected
        assert demands["ltx25"].ideal_replicas == expected


def test_ltx_capacity_aggregates_multi_run_work_and_ignores_legacy_run_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SALAD_AUTOSCALER_LTX25_TARGET_DRAIN_MINUTES", raising=False)
    config = load_predictive_autoscaler_config(
        stage_bindings=(_binding("ltx25", max_replicas=2),)
    )

    rows = _pending_rows(40)
    for index, row in enumerate(rows):
        row["run_id"] = "run-a" if index < 20 else "run-b"
    demand = build_global_demands(
        rows_by_stage={"ltx25": rows},
        runtime_by_stage={"ltx25": 200.0},
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )["ltx25"]
    assert demand.ideal_replicas == 2
    assert demand.run_counts == {"run-a": 20, "run-b": 20}

    tail = _pending_rows(29)
    for index, row in enumerate(tail):
        row["run_id"] = "run-a" if index < 15 else "run-b"
        row["run_job_count"] = 100
    tail_demand = build_global_demands(
        rows_by_stage={"ltx25": tail},
        runtime_by_stage={"ltx25": 200.0},
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )["ltx25"]
    assert tail_demand.ideal_replicas == 2
    assert tail_demand.run_counts == {"run-a": 15, "run-b": 14}
    assert tail_demand.jobs_per_replica is None
    assert tail_demand.job_count_target is None


def test_ltx_cold_start_is_excluded_from_inference_drain_horizon() -> None:
    stage = "ltx25"
    config = _config(
        (stage,),
        stage_max_replicas=4,
        target_drain_seconds=600.0,
        stage_target_drain_seconds={stage: 600.0},
        stage_cold_start_seconds={stage: 1934.0},
    )

    demands = build_global_demands(
        rows_by_stage={stage: _pending_rows(10)},
        runtime_by_stage={stage: 240.0},
        config=config,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )

    assert demands[stage].effective_drain_seconds == 600.0
    assert demands[stage].ideal_replicas == 4


def test_stage_specific_cold_start_changes_capacity_estimate() -> None:
    stages = ("qwen_image_21", "whisper")
    config = _config(
        stages,
        stage_max_replicas=4,
        stage_cold_start_seconds={
            "qwen_image_21": 500.0,
            "whisper": 0.0,
        },
    )

    demands = build_global_demands(
        rows_by_stage={
            "qwen_image_21": _pending_rows(6),
            "whisper": _pending_rows(6),
        },
        runtime_by_stage={
            "qwen_image_21": 100.0,
            "whisper": 100.0,
        },
        config=config,
        now=datetime(2026, 9, 24, tzinfo=UTC),
    )

    assert demands["qwen_image_21"].ideal_replicas == 4
    assert demands["whisper"].ideal_replicas == 1


def test_rebounded_demand_cancels_pending_instance_drains() -> None:
    stage = "ltx25"
    store = FakeStore(rows={stage: []}, runtimes={stage: [60.0]})
    client = FakeSaladClient(
        replicas=2,
        instances=[
            {"id": "idle-a", "deletion_cost": 0},
            {"id": "idle-b", "deletion_cost": 0},
        ],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=store,
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()[stage]
    assert first.reason == "drain_grace_pending"
    assert store.drains[stage]

    store.rows[stage] = _pending_rows(20)
    second = autoscaler.reconcile()[stage]

    assert second.target_replicas == 2
    assert store.drains.get(stage) in (None, {})
    assert client.replica_updates == []


def test_drain_protection_failure_marks_reconcile_as_failed() -> None:
    stage = "realesrgan"
    client = FakeSaladClient(
        replicas=1,
        instances=[{"id": "idle-instance", "deletion_cost": 100_000}],
        fail_deletion_cost=True,
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows={stage: []}, runtimes={stage: [53.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    with pytest.raises(
        AutoscalerReconciliationError,
        match="could not safely converge",
    ):
        autoscaler.reconcile()

    assert client.replica_updates == []


@pytest.mark.parametrize(
    ("instances", "expected_reason"),
    [
        (
            [{"id": "different-instance", "deletion_cost": 0}],
            "active_instance_mapping_incomplete",
        ),
        (
            [{"id": "instance-active", "deletion_cost": 100_000}],
            "instance_inventory_incomplete",
        ),
    ],
)
def test_persistent_safe_downscale_deferral_degrades_reconciliation(
    instances: list[dict[str, object]],
    expected_reason: str,
) -> None:
    stage = "ltx25"
    rows = {
        stage: [
            {
                "job_id": "job-running",
                "run_id": "run-a",
                "status": "running",
                "worker_id": "worker-instance-active",
                "started_at": None,
            }
        ]
    }
    client = FakeSaladClient(replicas=2, instances=instances)
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            (stage,),
            nonconvergence_failure_polls=3,
        ),
        store=FakeStore(rows=rows, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()[stage]
    second = autoscaler.reconcile()[stage]

    assert first.reason == expected_reason
    assert second.reason == expected_reason
    with pytest.raises(
        AutoscalerReconciliationError,
        match=expected_reason,
    ):
        autoscaler.reconcile()
    assert client.replica_updates == []



def test_nonconvergence_counter_resets_when_deferral_reason_changes() -> None:
    stage = "ltx25_i2v"
    store = FakeStore(
        rows={stage: [{
            "job_id": "job-running", "run_id": "run-a", "status": "running",
            "worker_id": "worker-instance-active", "started_at": None,
        }]},
        runtimes={stage: [60.0]},
    )
    client = FakeSaladClient(
        replicas=2, instances=[{"id": "different-instance", "deletion_cost": 0}],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,), nonconvergence_failure_polls=2),
        store=store,
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    assert (
        autoscaler.reconcile()[stage].reason
        == "active_instance_mapping_incomplete"
    )
    client.instances = [{"id": "instance-active", "deletion_cost": 100_000}]
    assert autoscaler.reconcile()[stage].reason == "instance_inventory_incomplete"
    with pytest.raises(
        AutoscalerReconciliationError, match="instance_inventory_incomplete",
    ):
        autoscaler.reconcile()
    assert client.stop_calls == 0
    assert client.replica_updates == []



def test_nonconvergence_counter_resets_when_capacity_target_recovers() -> None:
    stage = "ltx25"
    store = FakeStore(
        rows={
            stage: [
                {
                    "job_id": "job-running",
                    "run_id": "run-a",
                    "status": "running",
                    "worker_id": "worker-instance-active",
                    "started_at": None,
                }
            ]
        },
        runtimes={stage: [60.0]},
    )
    client = FakeSaladClient(
        replicas=2,
        instances=[{"id": "different-instance", "deletion_cost": 0}],
    )
    autoscaler = PredictiveSaladAutoscaler(
        config=_config(
            (stage,),
            nonconvergence_failure_polls=2,
        ),
        store=store,
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    first = autoscaler.reconcile()[stage]
    assert first.reason == "active_instance_mapping_incomplete"

    store.rows[stage] = _pending_rows(20)
    recovered = autoscaler.reconcile()[stage]
    assert recovered.target_replicas >= recovered.current_replicas

    store.rows[stage] = [
        {
            "job_id": "job-running-again",
            "run_id": "run-b",
            "status": "running",
            "worker_id": "worker-instance-active",
            "started_at": None,
        }
    ]
    deferred = autoscaler.reconcile()[stage]
    assert deferred.reason == "active_instance_mapping_incomplete"


def test_opt_in_cost_aware_controller_does_not_buy_useless_cold_replica() -> None:
    stage = "ltx25_i2v"
    client = FakeSaladClient(replicas=1, status="running")
    store = FakeStore(
        rows={stage: _pending_rows(10)},
        runtimes={stage: [100.0]},
    )
    config = replace(
        _config(
            (stage,),
            target_drain_seconds=600,
            stage_target_drain_seconds={stage: 600},
            stage_cold_start_seconds={stage: 1400},
        ),
        ltx_cost_aware=True,
    )
    scaler = PredictiveSaladAutoscaler(
        config=config,
        store=store,
        clients={stage: client},
        bindings={stage: _binding(stage)},
    )
    result = scaler.reconcile()[stage]
    assert result.target_replicas == 1
    assert client.replica_updates == []


def test_opt_in_cost_aware_controller_can_request_more_for_large_backlog() -> None:
    stage = "ltx25_i2v"
    client = FakeSaladClient(replicas=0, status="stopped")
    store = FakeStore(
        rows={stage: _pending_rows(52)},
        runtimes={stage: [235.0]},
    )
    config = replace(
        _config(
            (stage,),
            target_drain_seconds=3600,
            target_utilization=0.85,
            stage_target_drain_seconds={stage: 3600},
            stage_cold_start_seconds={stage: 900},
        ),
        ltx_cost_aware=True,
    )
    scaler = PredictiveSaladAutoscaler(
        config=config,
        store=store,
        clients={stage: client},
        bindings={stage: _binding(stage)},
    )
    result = scaler.reconcile()[stage]
    assert result.target_replicas == 4
    assert client.replica_updates == [4]
    assert result.reason == "resize_before_start_pending_confirmation"
