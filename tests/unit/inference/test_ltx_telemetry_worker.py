from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    ObjectInput,
    ObjectOutput,
)
from ai_video_factory.inference.ports import LocalArtifact
from ai_video_factory.inference.repository import InMemoryJobRepository
from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file
from ai_video_factory.inference.tasks import TaskRunnerRegistry
from ai_video_factory.inference.worker import InferenceWorker


class _VideoRunner:
    task_name = "video.ltx25.generate"

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        del request, inputs
        path = work_dir / "fake.mp4"
        path.write_bytes(b"mock-video")
        return LocalArtifact(
            path=path,
            content_type="video/mp4",
            telemetry={
                "pipeline_call_seconds": 9.0,
                "stage1_total_seconds": 5.0,
                "stage2_total_seconds": 3.0,
                "pipeline_reused": True,
            },
        )


class _RecordingRepository(InMemoryJobRepository):
    def __init__(self, *, fail_telemetry: bool) -> None:
        super().__init__()
        self.fail_telemetry = fail_telemetry
        self.records: list[dict[str, Any]] = []

    def record_ltx_telemetry(self, observation: Mapping[str, Any]) -> None:
        if self.fail_telemetry:
            raise RuntimeError("Missing telemetry migration")
        self.records.append(dict(observation))


@pytest.mark.parametrize("fail_telemetry", [False, True])
def test_completed_video_persists_telemetry_without_affecting_success(
    tmp_path: Path,
    fail_telemetry: bool,
) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    source = tmp_path / "source.jpg"
    source.write_bytes(b"mock-keyframe")
    storage.upload(
        source,
        "inputs/keyframe.jpg",
        content_type="image/jpeg",
        metadata={"artifact-sha256": sha256_file(source)},
    )
    request = InferenceJobRequest(
        job_id="ltx-observed-job",
        task="video.ltx25.generate",
        inputs=[ObjectInput(
            name="keyframe",
            key="inputs/keyframe.jpg",
            sha256=sha256_file(source),
        )],
        output=ObjectOutput(
            key="jobs/ltx-observed-job/output.mp4",
            content_type="video/mp4",
        ),
        parameters={
            "generation_profile": "pinned",
            "width": 1920,
            "height": 1080,
            "num_frames": 121,
        },
    )
    repository = _RecordingRepository(fail_telemetry=fail_telemetry)
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([_VideoRunner()]),
        worker_id="test-worker",
        salad_instance_id="gpu-test-instance",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    first = worker.process(request)
    assert first.replayed is False
    assert first.output.size_bytes > 0
    assert worker.process(request).replayed is True
    assert len(repository.records) == (0 if fail_telemetry else 1)
    if repository.records:
        observed = repository.records[0]
        assert observed["instance_id"] == "gpu-test-instance"
        assert observed["inference_seconds"] == 9.0
        assert observed["stage1_seconds"] == 5.0
        assert observed["stage2_seconds"] == 3.0
        assert observed["num_frames"] == 121
