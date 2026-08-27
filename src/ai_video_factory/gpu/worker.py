from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from .contracts import GPUJobRequest, GPUJobResponse, OutputArtifact
from .errors import (
    InputIntegrityError,
    JobBusyError,
    JobConflictError,
    JobExecutionError,
    LeaseLostError,
    UnsupportedTaskError,
)
from .ports import ClaimDecision, JobRepository, ObjectStorage, StoredObject
from .storage import sha256_file
from .tasks import TaskRunnerRegistry

logger = logging.getLogger(__name__)


class _LeaseHeartbeat:
    def __init__(
        self,
        repository: JobRepository,
        request: GPUJobRequest,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
        heartbeat_seconds: int,
    ) -> None:
        self._repository = repository
        self._request = request
        self._request_sha256 = request_sha256
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-{request.job_id}",
            daemon=True,
        )

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(self._heartbeat_seconds * 2, 1))

    def _run(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                self.renew_now()
            except Exception:
                logger.exception("lease heartbeat failed for job %s", self._request.job_id)
                if self._lost.is_set():
                    return

    def renew_now(self) -> None:
        renewed = self._repository.renew_lease(
            self._request.job_id,
            self._request_sha256,
            owner=self._owner,
            lease_seconds=self._lease_seconds,
        )
        if not renewed:
            self._lost.set()
            raise LeaseLostError(f"lease ownership was lost for job {self._request.job_id}")

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise LeaseLostError(f"lease ownership was lost for job {self._request.job_id}")


class GPUWorker:
    """Idempotent job executor with external object and transactional state."""

    def __init__(
        self,
        *,
        storage: ObjectStorage,
        repository: JobRepository,
        runners: TaskRunnerRegistry,
        worker_id: str,
        temp_dir: Path,
        lease_seconds: int = 90,
        heartbeat_seconds: int = 30,
    ) -> None:
        self.storage = storage
        self.repository = repository
        self.runners = runners
        self.worker_id = worker_id
        self.temp_dir = temp_dir
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def process(
        self,
        request: GPUJobRequest,
        *,
        transport_job_id: str | None = None,
    ) -> GPUJobResponse:
        request_sha256 = request.fingerprint()
        claim = self.repository.claim(
            request,
            request_sha256,
            owner=self.worker_id,
            lease_seconds=self.lease_seconds,
            transport_job_id=transport_job_id,
        )
        if claim.decision is ClaimDecision.REPLAY:
            if claim.result is None:
                raise JobConflictError(f"completed job has no stored result: {request.job_id}")
            return GPUJobResponse.model_validate(claim.result).model_copy(update={"replayed": True})
        if claim.decision is ClaimDecision.BUSY:
            raise JobBusyError(f"job is currently leased by another worker: {request.job_id}")

        try:
            response = self._execute_claimed(request, request_sha256, claim.attempt_count)
        except (JobConflictError, InputIntegrityError, LeaseLostError, UnsupportedTaskError) as exc:
            self._mark_failed(request, request_sha256, exc)
            raise
        except Exception as exc:
            self._mark_failed(request, request_sha256, exc)
            raise JobExecutionError(f"execution failed for job {request.job_id}") from exc

        self.repository.mark_succeeded(
            request.job_id,
            request_sha256,
            owner=self.worker_id,
            result=response.model_dump(mode="json"),
        )
        return response

    def _execute_claimed(
        self,
        request: GPUJobRequest,
        request_sha256: str,
        attempt_count: int,
    ) -> GPUJobResponse:
        existing = self.storage.stat(request.output.key)
        if existing is not None:
            output = self._reconcile_existing(request, request_sha256, existing)
            return GPUJobResponse(
                job_id=request.job_id,
                request_sha256=request_sha256,
                output=output,
                attempt_count=attempt_count,
                replayed=True,
            )

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        with _LeaseHeartbeat(
            self.repository,
            request,
            request_sha256,
            owner=self.worker_id,
            lease_seconds=self.lease_seconds,
            heartbeat_seconds=self.heartbeat_seconds,
        ) as heartbeat:
            with tempfile.TemporaryDirectory(
                prefix=f"{request.job_id}-",
                dir=self.temp_dir,
            ) as temporary:
                work_dir = Path(temporary)
                inputs = self._download_inputs(request, work_dir / "inputs")
                heartbeat.ensure_owned()

                runner = self.runners.get(request.task)
                artifact = runner.run(request, inputs, work_dir)
                self._validate_local_artifact(artifact.path, work_dir)
                if artifact.content_type != request.output.content_type:
                    raise ValueError("task output content type does not match the job contract")
                digest = sha256_file(artifact.path)
                heartbeat.renew_now()

                uploaded = self.storage.upload(
                    artifact.path,
                    request.output.key,
                    content_type=artifact.content_type,
                    metadata={
                        "job-id": request.job_id,
                        "request-sha256": request_sha256,
                        "artifact-sha256": digest,
                    },
                )
                heartbeat.ensure_owned()
                output = self._artifact_from_stored(request, digest, uploaded)

        return GPUJobResponse(
            job_id=request.job_id,
            request_sha256=request_sha256,
            output=output,
            attempt_count=attempt_count,
            replayed=False,
        )

    def _download_inputs(self, request: GPUJobRequest, input_dir: Path) -> dict[str, Path]:
        downloaded: dict[str, Path] = {}
        for index, item in enumerate(request.inputs):
            suffix = Path(item.key).suffix
            destination = input_dir / f"{index:02d}-{item.name}{suffix}"
            self.storage.download(item.key, destination)
            if item.sha256 is not None:
                actual = sha256_file(destination)
                if actual != item.sha256:
                    raise InputIntegrityError(
                        f"sha256 mismatch for input {item.name!r}: "
                        f"expected {item.sha256}, got {actual}"
                    )
            downloaded[item.name] = destination
        return downloaded

    @staticmethod
    def _validate_local_artifact(path: Path, work_dir: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(work_dir.resolve()):
            raise ValueError("task output must remain inside its temporary work directory")
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 1:
            raise ValueError("task runner produced an empty, missing or unsafe artifact")

    def _reconcile_existing(
        self,
        request: GPUJobRequest,
        request_sha256: str,
        stored: StoredObject,
    ) -> OutputArtifact:
        metadata = stored.metadata
        if (
            metadata.get("job-id") != request.job_id
            or metadata.get("request-sha256") != request_sha256
            or not metadata.get("artifact-sha256")
        ):
            from .errors import OutputConflictError

            raise OutputConflictError(
                f"output key already exists for a different request: {request.output.key}"
            )
        return self._artifact_from_stored(
            request,
            metadata["artifact-sha256"],
            stored,
        )

    @staticmethod
    def _artifact_from_stored(
        request: GPUJobRequest,
        expected_sha256: str,
        stored: StoredObject,
    ) -> OutputArtifact:
        actual_metadata_sha = stored.metadata.get("artifact-sha256")
        if actual_metadata_sha != expected_sha256:
            raise RuntimeError("uploaded output metadata does not match its local digest")
        if stored.size_bytes < 1:
            raise RuntimeError("uploaded output is empty")
        return OutputArtifact(
            key=stored.key,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=expected_sha256,
            etag=stored.etag,
        )

    def _mark_failed(
        self,
        request: GPUJobRequest,
        request_sha256: str,
        error: BaseException,
    ) -> None:
        try:
            self.repository.mark_failed(
                request.job_id,
                request_sha256,
                owner=self.worker_id,
                error=f"{type(error).__name__}: {error}",
            )
        except Exception:
            logger.exception("failed to persist job failure for %s", request.job_id)

    def ready(self) -> None:
        self.repository.ping()
        self.storage.ping()

    def close(self) -> None:
        self.repository.close()
