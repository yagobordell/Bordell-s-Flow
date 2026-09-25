from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from .bundle_publication import (
    load_committed_bundle,
    publish_committed_bundle,
    recover_committed_bundle,
    stage_bundle,
)
from .contracts import InferenceJobRequest, InferenceJobResponse, OutputArtifact
from .errors import (
    InputIntegrityError,
    JobBusyError,
    JobConflictError,
    JobExecutionError,
    LeaseLostError,
    NonRetryableTaskError,
    OutputConflictError,
    UnsupportedTaskError,
)
from .gpu_failures import cleanup_cuda_memory, is_retryable_gpu_failure
from .ports import (
    ClaimDecision,
    JobRepository,
    LocalSidecarArtifact,
    ObjectStorage,
    StoredObject,
)
from .storage import sha256_file
from .tasks import TaskRunnerRegistry

logger = logging.getLogger(__name__)


class _LeaseHeartbeat:
    def __init__(
        self,
        repository: JobRepository,
        request: InferenceJobRequest,
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


class InferenceWorker:
    """Idempotent inference executor with external object and transactional state."""

    def __init__(
        self,
        *,
        storage: ObjectStorage,
        repository: JobRepository,
        runners: TaskRunnerRegistry,
        worker_id: str,
        temp_dir: Path,
        salad_instance_id: str | None = None,
        lease_seconds: int = 90,
        heartbeat_seconds: int = 30,
        gpu_retry_cooldown_seconds: float = 60.0,
    ) -> None:
        self.storage = storage
        self.repository = repository
        self.runners = runners
        self.worker_id = worker_id
        self.salad_instance_id = str(salad_instance_id or "").strip() or None
        self.temp_dir = temp_dir
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.gpu_retry_cooldown_seconds = gpu_retry_cooldown_seconds
        self._execution_lock = threading.Lock()

    def prepare(self) -> None:
        self.runners.prepare()

    def process(
        self,
        request: InferenceJobRequest,
        *,
        transport_job_id: str | None = None,
    ) -> InferenceJobResponse:
        if not self._execution_lock.acquire(blocking=False):
            raise JobBusyError(
                f"worker {self.worker_id} is already executing another inference job"
            )
        try:
            return self._process_claimed_request(
                request,
                transport_job_id=transport_job_id,
            )
        finally:
            self._execution_lock.release()

    def _process_claimed_request(
        self,
        request: InferenceJobRequest,
        *,
        transport_job_id: str | None = None,
    ) -> InferenceJobResponse:
        if (
            self.salad_instance_id is not None
            and self.repository.is_instance_draining(self.salad_instance_id)
        ):
            raise JobBusyError(
                f"worker {self.worker_id} is draining and cannot claim new inference jobs"
            )

        request_sha256 = request.fingerprint()
        claim = self.repository.claim(
            request,
            request_sha256,
            owner=self.worker_id,
            lease_seconds=self.lease_seconds,
            transport_job_id=transport_job_id,
            instance_id=self.salad_instance_id,
        )
        if claim.decision is ClaimDecision.REPLAY:
            if claim.result is None:
                raise JobConflictError(f"completed job has no stored result: {request.job_id}")
            response = InferenceJobResponse.model_validate(claim.result)
            return self._reconcile_replayed_response(request, response)
        if claim.decision is ClaimDecision.BUSY:
            raise JobBusyError(f"job is currently leased by another worker: {request.job_id}")

        try:
            response = self._execute_claimed(request, request_sha256, claim.attempt_count)
        except LeaseLostError:
            # Ownership has already moved elsewhere. This worker must not mutate job state.
            raise
        except (
            JobConflictError,
            InputIntegrityError,
            NonRetryableTaskError,
            OutputConflictError,
            UnsupportedTaskError,
        ) as exc:
            self._mark_failed(
                request,
                request_sha256,
                exc,
                retryable=False,
            )
            raise
        except Exception as exc:
            retryable_gpu_failure = is_retryable_gpu_failure(exc)
            can_retry = claim.attempt_count < request.effective_max_attempts
            self._mark_failed(
                request,
                request_sha256,
                exc,
                retryable=can_retry,
            )
            if retryable_gpu_failure:
                cleanup_cuda_memory()
            if not can_retry:
                raise NonRetryableTaskError(
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            raise JobExecutionError(f"execution failed for job {request.job_id}") from exc

        self.repository.mark_succeeded(
            request.job_id,
            request_sha256,
            owner=self.worker_id,
            result=response.model_dump(mode="json"),
        )
        return response

    def _reconcile_replayed_response(
        self,
        request: InferenceJobRequest,
        response: InferenceJobResponse,
    ) -> InferenceJobResponse:
        request_sha256 = request.fingerprint()
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        existing = self.storage.stat(request.output.key)
        output: OutputArtifact | None = None

        if existing is not None:
            output = self._reconcile_existing(request, request_sha256, existing)
            try:
                self._reconcile_existing_sidecars(
                    request,
                    request_sha256,
                    primary_sha256=output.sha256,
                )
            except OutputConflictError:
                output = None

        if output is None:
            with tempfile.TemporaryDirectory(
                prefix=f"{request.job_id}-replay-",
                dir=self.temp_dir,
            ) as temporary:
                recovered = recover_committed_bundle(
                    self.storage,
                    request,
                    Path(temporary),
                )
            if recovered is None:
                raise OutputConflictError(
                    f"completed job artifact bundle is incomplete: {request.job_id}"
                )
            primary, _ = recovered
            output = self._reconcile_existing(request, request_sha256, primary)
            self._reconcile_existing_sidecars(
                request,
                request_sha256,
                primary_sha256=output.sha256,
            )

        expected = response.output
        if (
            output.key != expected.key
            or output.content_type != expected.content_type
            or output.size_bytes != expected.size_bytes
            or output.sha256 != expected.sha256
        ):
            raise JobConflictError(
                f"completed job result does not match durable artifact bundle: {request.job_id}"
            )
        return response.model_copy(update={"output": output, "replayed": True})

    def _execute_claimed(
        self,
        request: InferenceJobRequest,
        request_sha256: str,
        attempt_count: int,
    ) -> InferenceJobResponse:
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
                existing = self.storage.stat(request.output.key)
                authoritative_output: OutputArtifact | None = None

                if existing is not None:
                    output = self._reconcile_existing(request, request_sha256, existing)
                    try:
                        self._reconcile_existing_sidecars(
                            request,
                            request_sha256,
                            primary_sha256=output.sha256,
                        )
                    except OutputConflictError:
                        committed = load_committed_bundle(
                            self.storage,
                            request,
                            request_sha256,
                            work_dir,
                        )
                        if committed is not None:
                            heartbeat.renew_now()
                            primary, _ = publish_committed_bundle(
                                self.storage,
                                request,
                                request_sha256,
                                committed,
                                work_dir,
                            )
                            heartbeat.ensure_owned()
                            output = self._reconcile_existing(
                                request,
                                request_sha256,
                                primary,
                            )
                            self._reconcile_existing_sidecars(
                                request,
                                request_sha256,
                                primary_sha256=output.sha256,
                            )
                            return InferenceJobResponse(
                                job_id=request.job_id,
                                request_sha256=request_sha256,
                                output=output,
                                attempt_count=attempt_count,
                                replayed=True,
                            )

                        missing_sidecar = any(
                            self.storage.stat(contract.key) is None
                            for contract in (request.sidecar_outputs or {}).values()
                        )
                        if not missing_sidecar:
                            raise
                        authoritative_output = output
                    else:
                        return InferenceJobResponse(
                            job_id=request.job_id,
                            request_sha256=request_sha256,
                            output=output,
                            attempt_count=attempt_count,
                            replayed=True,
                        )
                else:
                    committed = load_committed_bundle(
                        self.storage,
                        request,
                        request_sha256,
                        work_dir,
                    )
                    if committed is not None:
                        heartbeat.renew_now()
                        primary, _ = publish_committed_bundle(
                            self.storage,
                            request,
                            request_sha256,
                            committed,
                            work_dir,
                        )
                        heartbeat.ensure_owned()
                        output = self._reconcile_existing(
                            request,
                            request_sha256,
                            primary,
                        )
                        self._reconcile_existing_sidecars(
                            request,
                            request_sha256,
                            primary_sha256=output.sha256,
                        )
                        return InferenceJobResponse(
                            job_id=request.job_id,
                            request_sha256=request_sha256,
                            output=output,
                            attempt_count=attempt_count,
                            replayed=True,
                        )

                inputs = self._download_inputs(request, work_dir / "inputs")
                heartbeat.ensure_owned()

                runner = self.runners.get(request.task)
                artifact = runner.run(request, inputs, work_dir)
                self._validate_local_artifact(artifact.path, work_dir)
                if artifact.content_type != request.output.content_type:
                    raise ValueError("task output content type does not match the job contract")

                sidecar_sources = self._validate_local_sidecars(
                    request,
                    artifact.sidecars,
                    work_dir,
                )
                digest = sha256_file(artifact.path)
                if authoritative_output is not None and digest != authoritative_output.sha256:
                    raise OutputConflictError(
                        "cannot repair missing legacy sidecars because regenerated primary "
                        f"differs from the authoritative output: {request.output.key}"
                    )

                heartbeat.renew_now()
                committed = stage_bundle(
                    self.storage,
                    request,
                    request_sha256,
                    primary_path=artifact.path,
                    primary_content_type=artifact.content_type,
                    sidecars={
                        name: (sidecar.path, sidecar.content_type)
                        for name, sidecar in sidecar_sources.items()
                    },
                    work_dir=work_dir,
                )
                heartbeat.ensure_owned()

                heartbeat.renew_now()
                local_sources: dict[str, Path] = {}
                if sha256_file(artifact.path) == committed.primary.sha256:
                    local_sources["primary"] = artifact.path
                for name, sidecar in sidecar_sources.items():
                    staged_sidecar = committed.sidecars[name]
                    if sha256_file(sidecar.path) == staged_sidecar.sha256:
                        local_sources[f"sidecar:{name}"] = sidecar.path
                primary, created = publish_committed_bundle(
                    self.storage,
                    request,
                    request_sha256,
                    committed,
                    work_dir,
                    local_sources=local_sources,
                )
                heartbeat.ensure_owned()

                output = self._reconcile_existing(request, request_sha256, primary)
                self._reconcile_existing_sidecars(
                    request,
                    request_sha256,
                    primary_sha256=output.sha256,
                )

        return InferenceJobResponse(
            job_id=request.job_id,
            request_sha256=request_sha256,
            output=output,
            attempt_count=attempt_count,
            replayed=not created,
        )

    def _download_inputs(
        self,
        request: InferenceJobRequest,
        input_dir: Path,
    ) -> dict[str, Path]:
        downloaded: dict[str, Path] = {}
        for index, item in enumerate(request.inputs):
            suffix = Path(item.key).suffix
            destination = input_dir / f"{index:02d}-{item.name}{suffix}"
            stored = self.storage.download(item.key, destination)
            if item.content_type is not None and stored.content_type != item.content_type:
                raise InputIntegrityError(
                    f"content type mismatch for input {item.name!r}: "
                    f"expected {item.content_type}, got {stored.content_type}"
                )
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

    def _validate_local_sidecars(
        self,
        request: InferenceJobRequest,
        sidecars: tuple[LocalSidecarArtifact, ...],
        work_dir: Path,
    ) -> dict[str, LocalSidecarArtifact]:
        declared = request.sidecar_outputs or {}
        returned = {item.name: item for item in sidecars}
        if set(returned) != set(declared):
            raise ValueError("task sidecar artifacts do not match the request contract")
        for name, contract in declared.items():
            artifact = returned[name]
            self._validate_local_artifact(artifact.path, work_dir)
            if artifact.content_type != contract.content_type:
                raise ValueError(
                    f"sidecar {name!r} content type does not match the request contract"
                )
        return returned

    def _reconcile_existing_sidecars(
        self,
        request: InferenceJobRequest,
        request_sha256: str,
        *,
        primary_sha256: str,
    ) -> None:
        for name, contract in (request.sidecar_outputs or {}).items():
            stored = self.storage.stat(contract.key)
            if stored is None:
                raise OutputConflictError(
                    f"primary output exists but sidecar {name!r} is missing: {contract.key}"
                )
            metadata = stored.metadata
            if (
                metadata.get("job-id") != request.job_id
                or metadata.get("request-sha256") != request_sha256
                or metadata.get("sidecar-name") != name
                or not metadata.get("artifact-sha256")
                or metadata.get("primary-artifact-sha256") != primary_sha256
                or stored.content_type != contract.content_type
                or stored.size_bytes < 1
            ):
                raise OutputConflictError(
                    f"sidecar output exists for a different request: {contract.key}"
                )

    def _reconcile_existing(
        self,
        request: InferenceJobRequest,
        request_sha256: str,
        stored: StoredObject,
    ) -> OutputArtifact:
        metadata = stored.metadata
        if (
            metadata.get("job-id") != request.job_id
            or metadata.get("request-sha256") != request_sha256
            or not metadata.get("artifact-sha256")
        ):
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
        request: InferenceJobRequest,
        expected_sha256: str,
        stored: StoredObject,
    ) -> OutputArtifact:
        actual_metadata_sha = stored.metadata.get("artifact-sha256")
        if actual_metadata_sha != expected_sha256:
            raise RuntimeError("uploaded output metadata does not match its local digest")
        if stored.size_bytes < 1:
            raise RuntimeError("uploaded output is empty")
        if stored.content_type != request.output.content_type:
            raise RuntimeError("uploaded output content type does not match the job contract")
        return OutputArtifact(
            key=stored.key,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=expected_sha256,
            etag=stored.etag,
        )

    def _mark_failed(
        self,
        request: InferenceJobRequest,
        request_sha256: str,
        error: BaseException,
        *,
        retryable: bool,
    ) -> None:
        try:
            self.repository.mark_failed(
                request.job_id,
                request_sha256,
                owner=self.worker_id,
                error=f"{type(error).__name__}: {error}",
                retryable=retryable,
            )
        except Exception:
            logger.exception("failed to persist job failure for %s", request.job_id)

    def next_pending_request(self) -> InferenceJobRequest | None:
        if (
            self.salad_instance_id is not None
            and self.repository.is_instance_draining(self.salad_instance_id)
        ):
            return None
        return self.repository.next_pending_request(self.runners.task_names)

    def ready(self) -> None:
        self.repository.ping()
        self.storage.ping()
        self.runners.ready()

    def close(self) -> None:
        self.repository.close()
