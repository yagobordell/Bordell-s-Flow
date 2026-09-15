from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.errors import NonRetryableTaskError
from ai_video_factory.inference.ports import LocalArtifact
from ai_video_factory.inference.repository import InMemoryJobRepository
from ai_video_factory.inference.storage import LocalObjectStorage
from ai_video_factory.inference.tasks import TaskRunnerRegistry
from ai_video_factory.inference.worker import InferenceWorker


class _RejectingRunner:
    task_name = "test.non_retryable"

    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        del request, inputs, work_dir
        self.calls += 1
        raise NonRetryableTaskError("deterministic provider rejection")


def _request() -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id="terminal-job-001",
        task="test.non_retryable",
        output=ObjectOutput(
            key="jobs/terminal-job-001/output.png",
            content_type="image/png",
        ),
        parameters={},
    )


def _worker(tmp_path: Path) -> tuple[InferenceWorker, _RejectingRunner]:
    runner = _RejectingRunner()
    worker = InferenceWorker(
        storage=LocalObjectStorage(tmp_path / "objects"),
        repository=InMemoryJobRepository(),
        runners=TaskRunnerRegistry([runner]),
        worker_id="terminal-worker",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    return worker, runner


def test_non_retryable_failure_is_cached_before_second_execution(tmp_path: Path) -> None:
    worker, runner = _worker(tmp_path)
    request = _request()

    with pytest.raises(NonRetryableTaskError, match="deterministic provider rejection"):
        worker.process(request, transport_job_id="salad-attempt-1")
    with pytest.raises(NonRetryableTaskError, match="deterministic provider rejection"):
        worker.process(request, transport_job_id="salad-attempt-2")

    assert runner.calls == 1


def test_non_retryable_failure_returns_422_without_reexecuting(tmp_path: Path) -> None:
    worker, runner = _worker(tmp_path)
    payload = _request().model_dump(mode="json", exclude_none=True)

    with TestClient(create_app(worker)) as client:
        first = client.post(
            "/jobs",
            headers={"Salad-Job-Id": "salad-attempt-1"},
            json=payload,
        )
        second = client.post(
            "/jobs",
            headers={"Salad-Job-Id": "salad-attempt-2"},
            json=payload,
        )

    assert first.status_code == 422
    assert second.status_code == 422
    assert "deterministic provider rejection" in first.json()["detail"]
    assert "deterministic provider rejection" in second.json()["detail"]
    assert runner.calls == 1
