from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from ai_video_factory.gpu.contracts import GPUJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.gpu.errors import (
    InputIntegrityError,
    JobBusyError,
    JobConflictError,
    LeaseLostError,
    OutputConflictError,
)
from ai_video_factory.gpu.ports import LocalArtifact, LocalArtifactPart
from ai_video_factory.gpu.repository import InMemoryJobRepository
from ai_video_factory.gpu.storage import LocalObjectStorage, sha256_file
from ai_video_factory.gpu.tasks import TaskRunnerRegistry
from ai_video_factory.gpu.worker import GPUWorker


class CountingCopyRunner:
    task_name = "infrastructure.copy"

    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        request: GPUJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        self.calls += 1
        output = work_dir / "result.txt"
        shutil.copyfile(inputs["source"], output)
        return LocalArtifact(output, request.output.content_type)


class LeaseLosingRepository(InMemoryJobRepository):
    def renew_lease(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> bool:
        return False


def seed(storage: LocalObjectStorage, path: Path, key: str, content: bytes) -> str:
    path.write_bytes(content)
    digest = sha256_file(path)
    storage.upload(
        path,
        key,
        content_type="text/plain",
        metadata={"artifact-sha256": digest},
    )
    return digest


def build_request(job_id: str, digest: str, *, parameters: dict[str, object] | None = None):
    return GPUJobRequest(
        job_id=job_id,
        task="infrastructure.copy",
        inputs=[ObjectInput(name="source", key="inputs/source.txt", sha256=digest)],
        output=ObjectOutput(key=f"jobs/{job_id}/output.txt", content_type="text/plain"),
        parameters=parameters or {},
    )


def build_worker(tmp_path: Path):
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = CountingCopyRunner()
    worker = GPUWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-1",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    return worker, storage, repository, runner


def test_worker_executes_once_and_replays_completed_result(tmp_path: Path) -> None:
    worker, storage, _, runner = build_worker(tmp_path)
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-replay", digest)

    first = worker.process(request, transport_job_id="salad-1")
    second = worker.process(request, transport_job_id="salad-1")

    assert first.replayed is False
    assert second.replayed is True
    assert first.output.sha256 == digest
    assert runner.calls == 1


def test_worker_rejects_same_job_id_with_different_request(tmp_path: Path) -> None:
    worker, storage, _, _ = build_worker(tmp_path)
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    worker.process(build_request("job-conflict", digest))

    with pytest.raises(JobConflictError, match="different request"):
        worker.process(build_request("job-conflict", digest, parameters={"changed": True}))


def test_worker_reconciles_output_after_database_commit_loss(tmp_path: Path) -> None:
    worker, storage, _, runner = build_worker(tmp_path)
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-reconcile", digest)
    existing = tmp_path / "existing.txt"
    existing.write_bytes(b"hello\n")
    storage.upload(
        existing,
        request.output.key,
        content_type="text/plain",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": digest,
        },
    )

    response = worker.process(request)

    assert response.replayed is True
    assert response.output.sha256 == digest
    assert runner.calls == 0


def test_worker_never_overwrites_foreign_output(tmp_path: Path) -> None:
    worker, storage, _, _ = build_worker(tmp_path)
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-output-conflict", digest)
    foreign = tmp_path / "foreign.txt"
    foreign.write_bytes(b"foreign\n")
    storage.upload(
        foreign,
        request.output.key,
        content_type="text/plain",
        metadata={
            "job-id": "other-job",
            "request-sha256": "0" * 64,
            "artifact-sha256": sha256_file(foreign),
        },
    )

    with pytest.raises(OutputConflictError, match="different request"):
        worker.process(request)


def test_worker_rejects_corrupted_input(tmp_path: Path) -> None:
    worker, storage, _, runner = build_worker(tmp_path)
    seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-corrupt", "0" * 64)

    with pytest.raises(InputIntegrityError, match="sha256 mismatch"):
        worker.process(request)
    assert runner.calls == 0


def test_active_lease_is_busy(tmp_path: Path) -> None:
    worker, storage, repository, _ = build_worker(tmp_path)
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-busy", digest)
    repository.claim(
        request,
        request.fingerprint(),
        owner="other-worker",
        lease_seconds=60,
        transport_job_id=None,
    )

    with pytest.raises(JobBusyError, match="leased"):
        worker.process(request)


def test_worker_does_not_upload_after_losing_lease(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    runner = CountingCopyRunner()
    worker = GPUWorker(
        storage=storage,
        repository=LeaseLosingRepository(),
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-1",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-lost-lease", digest)

    with pytest.raises(LeaseLostError, match="lost"):
        worker.process(request)
    assert storage.stat(request.output.key) is None



class SidecarCopyRunner:
    task_name = "infrastructure.copy-sidecar"

    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        request: GPUJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        self.calls += 1
        output = work_dir / "result.txt"
        metadata = work_dir / "metadata.json"
        shutil.copyfile(inputs["source"], output)
        metadata.write_text('{"kind":"sidecar"}\n', encoding="utf-8")
        return LocalArtifact(
            path=output,
            content_type="text/plain",
            sidecars={
                "metadata": LocalArtifactPart(
                    path=metadata,
                    content_type="application/json",
                )
            },
        )


def test_worker_uploads_and_replays_declared_sidecar_artifacts(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = SidecarCopyRunner()
    worker = GPUWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-sidecar",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = GPUJobRequest(
        job_id="job-sidecar",
        task=runner.task_name,
        inputs=[ObjectInput(name="source", key="inputs/source.txt", sha256=digest)],
        output=ObjectOutput(
            key="jobs/job-sidecar/output.txt",
            content_type="text/plain",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key="jobs/job-sidecar/metadata.json",
                content_type="application/json",
            )
        },
    )

    first = worker.process(request)
    second = worker.process(request)

    assert first.sidecar_outputs is not None
    assert first.sidecar_outputs["metadata"].key == "jobs/job-sidecar/metadata.json"
    assert storage.stat("jobs/job-sidecar/metadata.json") is not None
    assert second.replayed is True
    assert second.sidecar_outputs is not None
    assert runner.calls == 1
