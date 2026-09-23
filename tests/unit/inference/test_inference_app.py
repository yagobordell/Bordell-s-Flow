import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.repository import InMemoryJobRepository
from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file
from ai_video_factory.inference.tasks import CopyTaskRunner, TaskRunnerRegistry
from ai_video_factory.inference.worker import InferenceWorker


def test_http_worker_health_readiness_and_job(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    source = tmp_path / "source.txt"
    source.write_bytes(b"smoke\n")
    digest = sha256_file(source)
    storage.upload(
        source,
        "inputs/source.txt",
        content_type="text/plain",
        metadata={"artifact-sha256": digest},
    )
    worker = InferenceWorker(
        storage=storage,
        repository=InMemoryJobRepository(),
        runners=TaskRunnerRegistry([CopyTaskRunner()]),
        worker_id="http-worker",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )

    with TestClient(create_app(worker)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}
        response = client.post(
            "/jobs",
            headers={"Salad-Job-Id": "transport-123"},
            json={
                "schema_version": "1",
                "job_id": "http-job-001",
                "task": "infrastructure.copy",
                "inputs": [{"name": "source", "key": "inputs/source.txt", "sha256": digest}],
                "output": {
                    "key": "jobs/http-job-001/output.txt",
                    "content_type": "text/plain",
                },
                "parameters": {},
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["output"]["sha256"] == digest


class _RetryingPrepareWorker:
    def __init__(self) -> None:
        self.model_available = threading.Event()
        self.prepare_calls = 0
        self.ready_calls = 0
        self.closed = False

    def prepare(self) -> None:
        self.prepare_calls += 1
        if not self.model_available.is_set():
            raise ModelBootstrapPendingError("models are still downloading")

    def ready(self) -> None:
        self.ready_calls += 1

    def close(self) -> None:
        self.closed = True


def test_background_prepare_serves_health_while_models_are_missing() -> None:
    worker = _RetryingPrepareWorker()

    with TestClient(
        create_app(
            worker,  # type: ignore[arg-type]
            prepare_in_background=True,
            prepare_retry_seconds=0.01,
        )
    ) as client:
        deadline = time.monotonic() + 1
        while worker.prepare_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

        assert worker.prepare_calls >= 1
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").status_code == 503

        worker.model_available.set()
        response = client.get("/ready")
        deadline = time.monotonic() + 1
        while response.status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.01)
            response = client.get("/ready")

        assert response.json() == {"status": "ready"}
        assert worker.prepare_calls >= 2
        assert worker.ready_calls >= 1

    assert worker.closed is True
