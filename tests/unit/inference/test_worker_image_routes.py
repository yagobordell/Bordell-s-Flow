"""Regression tests for immutable concurrent LTX Docker lanes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_video_factory.inference.contracts import (
    InferenceJobRequest, ObjectOutput,
)
from ai_video_factory.inference.errors import UnsupportedTaskError
from ai_video_factory.inference.repository import InMemoryJobRepository
from ai_video_factory.inference.salad import SaladRequestError
from ai_video_factory.inference.salad_version_provision import (
    WorkerVersionProvisionError,
    WorkerVersionProvisioner,
)
from ai_video_factory.inference.worker_image_routes import (
    route_for_image,
)

PREFIX = "docker.io/yagobordell/ai-video-factory@sha256:"
A = PREFIX + "a" * 64
B = PREFIX + "b" * 64


def _job(name: str, image: str | None):
    parameters = {"required_worker_image": image} if image else {}
    return InferenceJobRequest(
        job_id=name, task="video.ltx25.audio_to_video",
        output=ObjectOutput(key=f"jobs/{name}/clip.mp4", content_type="video/mp4"),
        parameters=parameters,
    )


def test_two_digests_have_distinct_groups_but_same_digest_reuses_one():
    a = route_for_image("ltx25", A)
    b = route_for_image("ltx25", B)
    i2v = route_for_image("ltx25_i2v", A)
    assert a == route_for_image("ltx25", A)
    assert a.group_name != b.group_name
    assert a.group_name != i2v.group_name
    assert a.stage != b.stage
    assert a.stage != i2v.stage


@pytest.mark.parametrize("image", [
    "docker.io/foreign/worker@sha256:" + "a" * 64,
    "docker.io/yagobordell/ai-video-factory:latest",
    PREFIX + "f" * 63,
])
def test_unreviewed_or_mutable_image_cannot_request_paid_worker(image):
    with pytest.raises(ValueError, match="immutable digest"):
        route_for_image("ltx25", image)


def test_legacy_and_two_docker_versions_cannot_claim_each_others_jobs():
    repo = InMemoryJobRepository()
    for name, image in (("legacy", None), ("version-a", A), ("version-b", B)):
        request = _job(name, image)
        claim = repo.claim(
            request, request.fingerprint(), owner="seed", lease_seconds=30,
            transport_job_id=name,
        )
        repo.mark_failed(
            name, request.fingerprint(), owner="seed",
            error="temporary unavailable", retryable=True,
        )
        assert claim.decision.value == "start"

    assert repo.next_pending_request(("video.ltx25.audio_to_video",)).job_id == "legacy"
    assert repo.next_pending_request(
        ("video.ltx25.audio_to_video",), required_worker_image=A,
    ).job_id == "version-a"
    assert repo.next_pending_request(
        ("video.ltx25.audio_to_video",), required_worker_image=B,
    ).job_id == "version-b"


def _definition():
    return {
        "stack": {
            "shared_environment": {"INFERENCE_WORKER_MODE": "production"},
            "shared_required_environment": ["POSTGRES_DSN"],
            "container_port": 8080, "restart_policy": "always",
            "shared_liveness_probe": {
                "path": "/health", "period_seconds": 30,
                "failure_threshold": 20, "timeout_seconds": 10,
            },
        },
        "services": {"ltx25": {
            "environment": {"LTX_WORKER_TASK_MODE": "a2v"},
            "required_environment": ["HF_TOKEN"],
            "capacity": {"start_replicas": 1},
            "priority": "high",
            "resources": {
                "cpu": 8, "memory": 40960, "shm_size": 8192,
                "storage_amount": 171798691840,
                "gpu_class_names": ["RTX 5090 (32 GB)"],
            },
            "probes": {
                name: {
                    "path": path, "period_seconds": 15,
                    "failure_threshold": 20, "timeout_seconds": 5,
                } for name, path in (("startup", "/health"), ("readiness", "/ready"))
            },
        }},
    }


class FakeSalad:
    def __init__(self, image=None):
        self.group = {"container": {"image": image}} if image else None
        self.created = []

    def describe_container_group(self):
        if self.group is None:
            raise SaladRequestError("absent", 404, "url", "")
        return self.group

    def list_gpu_classes(self):
        return [{"id": "reviewed-gpu-id", "name": "RTX 5090 (32 GB)"}]

    def create_container_group(self, payload):
        assert self.group is None
        self.created.append(payload)
        self.group = {"container": {"image": payload["container"]["image"]}}


def test_controller_provisions_immutable_group_without_touching_existing(monkeypatch):
    fake = FakeSalad()
    controller = WorkerVersionProvisioner(
        document=_definition(), api_key="key", organization="org", project="project",
    )
    monkeypatch.setenv("POSTGRES_DSN", "postgres://shared")
    monkeypatch.setenv("HF_TOKEN", "private")
    monkeypatch.setattr(controller, "client", lambda _: fake)
    route = route_for_image("ltx25", A)
    assert controller.ensure(route, create=False) is False
    assert fake.created == []
    assert controller.ensure(route, create=True) is True
    assert controller.ensure(route, create=True) is True
    assert len(fake.created) == 1
    body = fake.created[0]
    assert body["name"] == route.group_name
    assert body["autostart_policy"] is False
    assert body["container"]["image"] == A
    assert body["container"]["environment_variables"]["INFERENCE_WORKER_IMAGE"] == A
    assert body["container"]["environment_variables"]["LTX_WORKER_TASK_MODE"] == "a2v"
    assert body["container"]["resources"]["gpu_classes"] == ["reviewed-gpu-id"]


def test_controller_never_mutates_a_group_owned_by_another_version(monkeypatch):
    fake = FakeSalad(image=B)
    controller = WorkerVersionProvisioner(
        document=_definition(), api_key="key", organization="org", project="project",
    )
    monkeypatch.setattr(controller, "client", lambda _: fake)
    with pytest.raises(WorkerVersionProvisionError, match="unexpected Docker image"):
        controller.ensure(route_for_image("ltx25", A), create=True)
    assert fake.created == []
