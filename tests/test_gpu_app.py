from pathlib import Path

from fastapi.testclient import TestClient

from ai_video_factory.gpu.app import create_app
from ai_video_factory.gpu.repository import InMemoryJobRepository
from ai_video_factory.gpu.storage import LocalObjectStorage, sha256_file
from ai_video_factory.gpu.tasks import TaskRunnerRegistry
from ai_video_factory.gpu.worker import GPUWorker


def test_http_worker_health_readiness_and_job(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    source = tmp_path / "source.txt"
    source.write_bytes(b"phase 7\n")
    digest = sha256_file(source)
    storage.upload(
        source,
        "inputs/source.txt",
        content_type="text/plain",
        metadata={"artifact-sha256": digest},
    )
    worker = GPUWorker(
        storage=storage,
        repository=InMemoryJobRepository(),
        runners=TaskRunnerRegistry.phase7(),
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
