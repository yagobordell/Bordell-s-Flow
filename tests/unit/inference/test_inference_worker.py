from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from ai_video_factory.inference.bundle_publication import bundle_manifest_key
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.errors import (
    InputIntegrityError,
    JobBusyError,
    JobConflictError,
    JobExecutionError,
    LeaseLostError,
    NonRetryableTaskError,
    OutputConflictError,
)
from ai_video_factory.inference.ports import LocalArtifact, LocalSidecarArtifact
from ai_video_factory.inference.repository import InMemoryJobRepository
from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file
from ai_video_factory.inference.tasks import TaskRunnerRegistry
from ai_video_factory.inference.worker import InferenceWorker


class CountingCopyRunner:
    task_name = "infrastructure.copy"

    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        self.calls += 1
        output = work_dir / "result.txt"
        shutil.copyfile(inputs["source"], output)
        return LocalArtifact(output, request.output.content_type)


class FailingRunner:
    task_name = "infrastructure.copy"

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
        raise RuntimeError("single-shot failure")


class _StrictParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str


class ValidationFailingRunner:
    task_name = "infrastructure.copy"

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
        _StrictParameters.model_validate({"prompt": "ok", "run_id": "invalid-extra"})
        raise AssertionError("validation must fail")


class DrainingRepository(InMemoryJobRepository):
    def is_instance_draining(self, instance_id: str) -> bool:
        assert instance_id == "instance-draining"
        return True

    def next_pending_request(self, task_names: tuple[str, ...]) -> InferenceJobRequest | None:
        raise AssertionError(f"draining worker must not poll jobs: {task_names}")


class DrainAppearsBeforeClaimRepository(InMemoryJobRepository):
    def __init__(self) -> None:
        super().__init__()
        self.drain_checks = 0

    def is_instance_draining(self, instance_id: str) -> bool:
        assert instance_id == "instance-race"
        self.drain_checks += 1
        return self.drain_checks >= 2


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
    return InferenceJobRequest(
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
    worker = InferenceWorker(
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


def test_worker_max_attempts_prevents_second_inference_execution(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = FailingRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-1",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-single-shot", digest).model_copy(
        update={"max_attempts": 1}
    )

    with pytest.raises(
        NonRetryableTaskError,
        match="RuntimeError: single-shot failure",
    ):
        worker.process(request, transport_job_id="salad-1")
    with pytest.raises(
        NonRetryableTaskError,
        match="RuntimeError: single-shot failure",
    ):
        worker.process(request, transport_job_id="salad-2")

    assert runner.calls == 1


def test_worker_validation_error_is_terminal_on_first_attempt(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = ValidationFailingRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-1",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-invalid-contract", digest)

    with pytest.raises(ValidationError, match="extra_forbidden"):
        worker.process(request)
    with pytest.raises(NonRetryableTaskError, match="extra_forbidden"):
        worker.process(request)

    assert runner.calls == 1


def test_worker_default_retry_budget_stops_after_five_attempts(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = FailingRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-1",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-default-retries", digest)

    for _ in range(4):
        with pytest.raises(JobExecutionError, match="execution failed"):
            worker.process(request)

    with pytest.raises(NonRetryableTaskError, match="single-shot failure"):
        worker.process(request)
    with pytest.raises(NonRetryableTaskError, match="single-shot failure"):
        worker.process(request)

    assert runner.calls == 5



def test_ltx_a2v_oom_has_only_one_clean_retry(tmp_path: Path) -> None:
    """Do not burn five identical A2V attempts while the GPU stays OOM."""
    class OomRunner:
        task_name = "video.ltx25.audio_to_video"

        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, inputs, work_dir):
            del request, inputs, work_dir
            self.calls += 1
            raise RuntimeError("AcceleratorError: CUDA error: out of memory")

    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = OomRunner()
    worker = InferenceWorker(
        storage=storage, repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-1", temp_dir=tmp_path / "temp",
        lease_seconds=60, heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("a2v-oom-twice", digest).model_copy(
        update={"task": runner.task_name, "max_attempts": 5}
    )
    with pytest.raises(JobExecutionError, match="execution failed"):
        worker.process(request)
    with pytest.raises(NonRetryableTaskError, match="out of memory"):
        worker.process(request)
    with pytest.raises(NonRetryableTaskError, match="out of memory"):
        worker.process(request)
    assert runner.calls == 2

def test_ltx_a2v_unsatisfiable_diffvae_budget_never_retries(tmp_path: Path) -> None:
    class ImpossibleTileRunner:
        task_name = "video.ltx25.audio_to_video"

        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, inputs, work_dir):
            del request, inputs, work_dir
            self.calls += 1
            raise ValueError(
                "Cannot fit a DiffVAE decode tile under the memory budget: "
                "stage4_feature_bytes=1022361600, usable_bytes=0."
            )

    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = ImpossibleTileRunner()
    worker = InferenceWorker(
        storage=storage, repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-1", temp_dir=tmp_path / "temp",
        lease_seconds=60, heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\\n")
    request = build_request("a2v-impossible-tile", digest).model_copy(
        update={"task": runner.task_name, "max_attempts": 5}
    )
    with pytest.raises(NonRetryableTaskError, match="Cannot fit a DiffVAE decode tile"):
        worker.process(request)
    with pytest.raises(NonRetryableTaskError, match="Cannot fit a DiffVAE decode tile"):
        worker.process(request)
    assert runner.calls == 1


def test_a2v_failure_has_persistent_phase_and_r2_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_video_factory.inference import gpu_diagnostics

    monkeypatch.setattr(gpu_diagnostics, "_gpu_identity", lambda: {})
    monkeypatch.setattr(gpu_diagnostics, "_cuda_snapshot", lambda: {"available": False})
    monkeypatch.setattr(gpu_diagnostics, "_read_linux_memory", lambda: {})

    class DiagnosticOomRunner:
        task_name = "video.ltx25.audio_to_video"

        def run(self, request, inputs, work_dir):
            del request, inputs, work_dir
            gpu_diagnostics.diagnostic_checkpoint("reference.gemma.prompt_encode.start")
            raise RuntimeError("AcceleratorError: CUDA error: out of memory")

    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([DiagnosticOomRunner()]),
        worker_id="worker-1", temp_dir=tmp_path / "temp",
        lease_seconds=60, heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\\n")
    request = build_request("a2v-diagnostic-oom", digest).model_copy(
        update={"task": DiagnosticOomRunner.task_name, "max_attempts": 1}
    )
    with pytest.raises(NonRetryableTaskError, match="out of memory"):
        worker.process(request)

    last_error = repository._jobs[request.job_id].last_error
    assert last_error is not None
    assert "diagnostic_phase=reference.gemma.prompt_encode.start" in last_error
    assert "diagnostic_r2=diagnostics/ltx-a2v/a2v-diagnostic-oom/" in last_error
    key = (
        "diagnostics/ltx-a2v/a2v-diagnostic-oom/"
        "attempt-01/003-failed.json"
    )
    assert storage.stat(key) is not None


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


def test_input_integrity_failure_is_terminal_without_retry(tmp_path: Path) -> None:
    worker, storage, _, runner = build_worker(tmp_path)
    seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-corrupt-terminal", "0" * 64)

    with pytest.raises(InputIntegrityError, match="sha256 mismatch"):
        worker.process(request)
    with pytest.raises(NonRetryableTaskError, match="InputIntegrityError"):
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
    worker = InferenceWorker(
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


class SidecarRunner:
    task_name = "test.sidecar"

    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        self.calls += 1
        output = work_dir / "result.txt"
        metadata = work_dir / "metadata.json"
        shutil.copyfile(inputs["source"], output)
        metadata.write_text('{"ok":true}\n', encoding="utf-8")
        return LocalArtifact(
            output,
            request.output.content_type,
            sidecars=(
                LocalSidecarArtifact(
                    name="metadata",
                    path=metadata,
                    content_type="application/json",
                ),
            ),
        )


class FailFinalPrimaryOnceStorage(LocalObjectStorage):
    def __init__(self, root: Path, *, final_primary_key: str) -> None:
        super().__init__(root)
        self.final_primary_key = final_primary_key
        self.failed_primary_publication = False

    def create_if_absent(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ):
        if key == self.final_primary_key and not self.failed_primary_publication:
            self.failed_primary_publication = True
            raise RuntimeError("simulated primary publication interruption")
        return super().create_if_absent(
            source,
            key,
            content_type=content_type,
            metadata=metadata,
        )


class DivergentSidecarRunner(SidecarRunner):
    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        artifact = super().run(request, inputs, work_dir)
        artifact.path.write_bytes(b"different\n")
        return artifact


def test_worker_uploads_and_reconciles_declared_sidecars(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = SidecarRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-sidecar",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    job_id = "job-sidecar"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.sidecar",
        inputs=[ObjectInput(name="source", key="inputs/source.txt", sha256=digest)],
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.txt",
            content_type="text/plain",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
    )

    first = worker.process(request)
    second = worker.process(request)

    assert first.replayed is False
    assert second.replayed is True
    metadata = storage.stat(f"jobs/{job_id}/metadata.json")
    assert metadata is not None
    assert metadata.content_type == "application/json"
    assert metadata.metadata["job-id"] == job_id
    assert metadata.metadata["sidecar-name"] == "metadata"
    assert metadata.metadata["request-sha256"] == request.fingerprint()


def test_worker_repairs_missing_sidecar_without_overwriting_primary(
    tmp_path: Path,
) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = SidecarRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-sidecar-repair",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    job_id = "job-sidecar-repair"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.sidecar",
        inputs=[ObjectInput(name="source", key="inputs/source.txt", sha256=digest)],
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.txt",
            content_type="text/plain",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
    )

    authoritative = tmp_path / "authoritative.txt"
    authoritative.write_bytes(b"hello\n")
    authoritative_sha = sha256_file(authoritative)
    storage.create_if_absent(
        authoritative,
        request.output.key,
        content_type="text/plain",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": authoritative_sha,
        },
    )

    response = worker.process(request)

    assert response.replayed is True
    assert response.output.sha256 == authoritative_sha
    assert runner.calls == 1
    metadata = storage.stat(request.sidecar_outputs["metadata"].key)
    assert metadata is not None
    assert metadata.metadata["primary-artifact-sha256"] == authoritative_sha

    downloaded = tmp_path / "downloaded.txt"
    storage.download(request.output.key, downloaded)
    assert downloaded.read_bytes() == b"hello\n"


def test_draining_salad_instance_does_not_claim_new_jobs(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    runner = CountingCopyRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=DrainingRepository(),
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-instance-draining",
        salad_instance_id="instance-draining",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )

    assert worker.next_pending_request() is None


def test_draining_salad_instance_rechecks_before_direct_claim(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = DrainingRepository()
    runner = CountingCopyRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-instance-draining",
        salad_instance_id="instance-draining",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-drain-race", digest)

    with pytest.raises(JobBusyError, match="draining"):
        worker.process(request)

    assert runner.calls == 0


def test_claim_rechecks_drain_after_worker_precheck(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = DrainAppearsBeforeClaimRepository()
    runner = CountingCopyRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-instance-race",
        salad_instance_id="instance-race",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source-race.txt", "inputs/source.txt", b"hello\n")
    request = build_request("job-drain-atomic-race", digest)

    with pytest.raises(JobBusyError, match="draining"):
        worker.process(request)

    assert repository.drain_checks == 2
    assert runner.calls == 0


def test_missing_sidecar_repair_rejects_divergent_regeneration(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    repository = InMemoryJobRepository()
    runner = DivergentSidecarRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-sidecar-divergent",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(storage, tmp_path / "source-divergent.txt", "inputs/source.txt", b"hello\n")
    job_id = "job-sidecar-divergent"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.sidecar",
        inputs=[ObjectInput(name="source", key="inputs/source.txt", sha256=digest)],
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.txt",
            content_type="text/plain",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
    )

    authoritative = tmp_path / "authoritative-divergent.txt"
    authoritative.write_bytes(b"hello\n")
    authoritative_sha = sha256_file(authoritative)
    storage.create_if_absent(
        authoritative,
        request.output.key,
        content_type="text/plain",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": authoritative_sha,
        },
    )

    with pytest.raises(OutputConflictError, match="regenerated primary"):
        worker.process(request)

    assert runner.calls == 1
    assert storage.stat(request.sidecar_outputs["metadata"].key) is None


def test_worker_recovers_committed_bundle_after_primary_publication_interruption(
    tmp_path: Path,
) -> None:
    job_id = "job-sidecar-interrupted-primary"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.sidecar",
        inputs=[],
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.txt",
            content_type="text/plain",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
    )
    storage = FailFinalPrimaryOnceStorage(
        tmp_path / "objects",
        final_primary_key=request.output.key,
    )
    repository = InMemoryJobRepository()

    class NoInputSidecarRunner(SidecarRunner):
        def run(
            self,
            request: InferenceJobRequest,
            inputs: Mapping[str, Path],
            work_dir: Path,
        ) -> LocalArtifact:
            del inputs
            self.calls += 1
            output = work_dir / "result.txt"
            metadata = work_dir / "metadata.json"
            output.write_bytes(b"first-attempt-primary\n")
            metadata.write_text('{"attempt":1}\n', encoding="utf-8")
            return LocalArtifact(
                output,
                request.output.content_type,
                sidecars=(
                    LocalSidecarArtifact(
                        name="metadata",
                        path=metadata,
                        content_type="application/json",
                    ),
                ),
            )

    runner = NoInputSidecarRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-bundle-recovery",
        temp_dir=tmp_path / "temp",
        lease_seconds=60,
        heartbeat_seconds=10,
    )

    with pytest.raises(JobExecutionError, match="execution failed"):
        worker.process(request)

    assert runner.calls == 1
    assert storage.stat(request.output.key) is None
    assert storage.stat(request.sidecar_outputs["metadata"].key) is not None
    assert storage.stat(bundle_manifest_key(request, request.fingerprint())) is not None

    response = worker.process(request)

    assert response.replayed is True
    assert runner.calls == 1
    assert storage.stat(request.output.key) is not None
    sidecar = storage.stat(request.sidecar_outputs["metadata"].key)
    assert sidecar is not None
    assert sidecar.metadata["primary-artifact-sha256"] == response.output.sha256


def test_worker_replay_repairs_deleted_sidecar_from_committed_bundle(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects-replay-repair")
    repository = InMemoryJobRepository()
    runner = SidecarRunner()
    worker = InferenceWorker(
        storage=storage,
        repository=repository,
        runners=TaskRunnerRegistry([runner]),
        worker_id="worker-sidecar-replay-repair",
        temp_dir=tmp_path / "temp-replay-repair",
        lease_seconds=60,
        heartbeat_seconds=10,
    )
    digest = seed(
        storage,
        tmp_path / "source-replay-repair.txt",
        "inputs/source-replay-repair.txt",
        b"hello\n",
    )
    job_id = "job-sidecar-replay-repair"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.sidecar",
        inputs=[
            ObjectInput(
                name="source",
                key="inputs/source-replay-repair.txt",
                sha256=digest,
            )
        ],
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.txt",
            content_type="text/plain",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
    )

    first = worker.process(request)
    storage._path(request.sidecar_outputs["metadata"].key).unlink()

    replay = worker.process(request)

    assert first.replayed is False
    assert replay.replayed is True
    assert runner.calls == 1
    assert storage.stat(request.sidecar_outputs["metadata"].key) is not None
