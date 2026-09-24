from __future__ import annotations

from datetime import UTC, datetime

from ai_video_factory.inference.salad_autoscaler import (
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

    def list_stage_jobs(self, *, binding, statuses):
        return [
            dict(row)
            for row in self.rows.get(binding.workload, [])
            if row.get("status") in statuses
        ]

    def list_recent_stage_runtime_seconds(self, *, binding, limit):
        return list(self.runtimes.get(binding.workload, []))[:limit]


class FakeSaladClient:
    def __init__(
        self,
        *,
        replicas: int,
        status: str = "running",
        instances: list[dict[str, object]] | None = None,
    ) -> None:
        self.replicas = replicas
        self.status = status
        self.instances = instances or []
        self.replica_updates: list[int] = []
        self.deletion_cost_updates: list[tuple[str, int]] = []
        self.start_calls = 0

    def describe_container_group(self) -> dict[str, object]:
        return {
            "replicas": self.replicas,
            "current_state": {"status": self.status},
        }

    def set_container_group_replicas(self, replicas: int) -> dict[str, object]:
        self.replica_updates.append(replicas)
        self.replicas = replicas
        return {"replicas": replicas}

    def start_container_group_if_needed(self, *, warning_logger=None) -> bool:
        self.start_calls += 1
        self.status = "deploying"
        return True

    def list_container_group_instances(self) -> list[dict[str, object]]:
        return [dict(item) for item in self.instances]

    def set_container_group_instance_deletion_cost(
        self,
        instance_id: str,
        deletion_cost: int,
    ) -> dict[str, object]:
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
        max_upscale_per_poll=10,
        max_downscale_per_poll=10,
        active_deletion_cost=100_000,
        idle_deletion_cost=0,
        stage_max_replicas={stage: stage_max_replicas for stage in stages},
        fallback_runtime_seconds={stage: 60.0 for stage in stages},
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


def test_stopped_group_scales_and_starts_when_postgres_has_demand() -> None:
    stage = "ltx25"
    client = FakeSaladClient(replicas=0, status="stopped")
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows={stage: _pending_rows(2)}, runtimes={stage: [60.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.applied_replicas == 1
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

    result = autoscaler.reconcile()[stage]

    assert result.applied_replicas == 1
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


def test_empty_global_queue_scales_replicas_to_zero() -> None:
    stage = "realesrgan"
    client = FakeSaladClient(replicas=1)
    autoscaler = PredictiveSaladAutoscaler(
        config=_config((stage,)),
        store=FakeStore(rows={stage: []}, runtimes={stage: [53.0]}),
        clients={stage: client},
        bindings={stage: _binding(stage)},
        logger=lambda _message: None,
    )

    result = autoscaler.reconcile()[stage]

    assert result.target_replicas == 0
    assert result.applied_replicas == 0
    assert client.replica_updates == [0]
