"""Singleton controller registration and quota tests for competing Docker versions."""

from __future__ import annotations

from pathlib import Path

from ai_video_factory.inference import salad_capacity
from ai_video_factory.inference.salad_autoscaler import (
    allocate_project_quota,
    build_global_demands,
)
from ai_video_factory.inference.worker_image_routes import route_for_image

PREFIX = "docker.io/yagobordell/ai-video-factory@sha256:"


class FakeStore:
    def __init__(self, _dsn):
        self.seen = []

    def list_stage_jobs(self, *, binding, statuses):
        self.seen.append((binding.workload, binding.required_worker_image, statuses))
        if binding.required_worker_image is None:
            return []
        return [{
            "job_id": binding.workload + "-job",
            "run_id": binding.workload,
            "status": "pending",
            "worker_id": None,
            "started_at": None,
        }]

    def close(self):
        pass


class FakeProvisioner:
    created = []

    def __init__(self, **_kwargs):
        pass

    def ensure(self, route, *, create):
        assert create is True
        self.created.append(route)
        return True

    def client(self, route):
        return route


def test_singleton_adds_both_a2v_versions_without_modifying_default_groups(monkeypatch):
    monkeypatch.setenv("SALAD_ORGANIZATION", "my-org")
    monkeypatch.setenv("SALAD_PROJECT", "my-project")
    monkeypatch.setenv("SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS", "2")
    monkeypatch.setattr(salad_capacity, "PostgresAutoscalerStore", FakeStore)
    monkeypatch.setattr(
        salad_capacity, "WorkerVersionProvisioner", FakeProvisioner,
    )
    monkeypatch.setattr(
        salad_capacity, "list_routes",
        lambda _dsn: [
            route_for_image("ltx25", PREFIX + "a" * 64),
            route_for_image("ltx25", PREFIX + "b" * 64),
        ],
    )
    FakeProvisioner.created = []
    runtime = salad_capacity.build_salad_capacity_runtime(
        services_path=Path("deploy/salad/services.json"),
        postgres_dsn="postgres://shared",
        salad_api_key="token",
        logger=lambda _msg: None,
    )
    original = set(runtime.autoscaler.clients)
    assert "ltx25" in original and "ltx25_i2v" in original
    runtime.autoscaler.route_refresher()
    route_a, route_b = FakeProvisioner.created
    for route in (route_a, route_b):
        assert route.stage in runtime.autoscaler.clients
        assert runtime.autoscaler.bindings[route.stage].required_worker_image == route.image
        assert route.group_name in runtime.autoscaler.managed_group_names
        assert runtime.autoscaler.config.stage_max_replicas[route.stage] == 4
        assert runtime.autoscaler.config.stage_jobs_per_replica[route.stage] == 30
    assert original.issubset(set(runtime.autoscaler.clients))
    assert runtime.autoscaler.config.project_max_replicas == 2

    # Both requests receive separate demand accounting, not one blended
    # shared-group backlog. Both still compete for a two-GPU project quota.
    rows = {
        stage: runtime.store.list_stage_jobs(
            binding=runtime.autoscaler.bindings[stage],
            statuses=("pending", "running", "retryable_failed"),
        ) for stage in (route_a.stage, route_b.stage)
    }
    config = runtime.autoscaler.config
    demands = build_global_demands(
        rows_by_stage=rows,
        runtime_by_stage={stage: 206.0 for stage in rows},
        config=config,
    )
    quotas = allocate_project_quota(
        demands=demands, project_max_replicas=config.project_max_replicas,
    )
    assert set(quotas) == {route_a.stage, route_b.stage}
    assert all(amount == 1 for amount in quotas.values())
    assert sum(quotas.values()) <= config.project_max_replicas
