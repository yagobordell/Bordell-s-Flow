from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_video_factory.inference.salad_autoscaler import (
    AutoscalerReconciliationError,
    AutoscalerServiceBinding,
    PredictiveAutoscalerConfig,
    PredictiveSaladAutoscaler,
    allocate_project_quota,
    build_global_demands,
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
        self.replica_updates: list[int] = []
        self.deletion_cost_updates: list[tuple[str, int]] = []
        self.start_calls = 0
        self.stop_calls = 0

    def list_project_container_groups(self) -> list[dict[str, object]]:
        return [dict(item) for item in self.project_groups]

    def describe_container_group(self) -> dict[str, object]:
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
    downscale_stable_polls: int = 1,
    nonconvergence_failure_polls: int = 4,
    provider_pending_max_seconds: float = 7200.0,
    stage_cold_start_seconds: dict[str, float] | None = None,
) -> PredictiveAutoscalerConfig:
    return PredictiveAutoscalerConfig(
        enabled=True,
        project_max_replicas=project_max_replicas,
        target_drain_seconds=600.0,
        cold_start_seconds=0.0,
        target_utilization=1.0,
        runtime_percentile=0.75,
        runtime_history_limit=200,
        downscale_stable_polls=downscale_stable_polls,
        nonconvergence_failure_polls=nonconvergence_failure_polls,
        max_upscale_per_poll=10,
        max_downscale_per_poll=10,
        active_deletion_cost=100_000,
        idle_deletion_cost=0,
        stage_max_replicas={stage: stage_max_replicas for stage in stages},
        fallback_runtime_seconds={stage: 60.0 for stage in stages},
        stage_cold_start_seconds=stage_cold_start_seconds,
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
            "insufficient_idle_instances",
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
