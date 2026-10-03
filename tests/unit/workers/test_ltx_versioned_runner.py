"""Unit safety tests for the immutable LTX queue-task adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.ports import LocalArtifact
from ai_video_factory.inference.worker_image_routes import route_for_image
from ai_video_factory.workers.ltx25.versioned_runner import VersionedLTXRunner

PREFIX = "docker.io/yagobordell/ai-video-factory@sha256:"
A = PREFIX + "a" * 64
B = PREFIX + "b" * 64


class FakeBaseRunner:
    task_name = "video.ltx25.audio_to_video"

    def __init__(self):
        self.calls = []

    def run(self, request, inputs, work_dir):
        assert request.task == self.task_name
        assert "required_worker_image" not in request.parameters
        self.calls.append(request)
        return LocalArtifact(path=work_dir / "result.mp4", content_type="video/mp4")


def request(image):
    route = route_for_image("ltx25", image)
    return InferenceJobRequest(
        job_id="isolated-request", task=route.task_name,
        output=ObjectOutput(
            key="jobs/isolated-request/result.mp4", content_type="video/mp4",
        ),
        parameters={"required_worker_image": image, "generation_profile": "reviewed"},
    )


def test_alias_removes_only_routing_metadata_not_generation_recipe(tmp_path: Path):
    base = FakeBaseRunner()
    runner = VersionedLTXRunner(
        runner=base, task_name=route_for_image("ltx25", A).task_name, image=A,
    )
    original = request(A)
    fingerprint = original.fingerprint()
    output = runner.run(original, {}, tmp_path)
    assert output.content_type == "video/mp4"
    assert base.calls[0].parameters == {"generation_profile": "reviewed"}
    assert original.fingerprint() == fingerprint
    assert original.parameters["required_worker_image"] == A


def test_foreign_image_never_reaches_the_gpu_backend(tmp_path: Path):
    base = FakeBaseRunner()
    runner = VersionedLTXRunner(
        runner=base, task_name=route_for_image("ltx25", A).task_name, image=A,
    )
    with pytest.raises(ValueError, match="another Docker version"):
        runner.run(request(B), {}, tmp_path)
    assert base.calls == []
