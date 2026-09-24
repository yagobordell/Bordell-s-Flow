from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    InferenceJobResponse,
    OutputArtifact,
)
from ai_video_factory.inference.ports import ObjectStorage, StoredObject
from ai_video_factory.inference.storage import sha256_file

from .job_queue import JobQueueClient, QueueJobStatus, TransientQueueError

logger = logging.getLogger(__name__)


class InferenceJobTimeoutError(TimeoutError):
    """A queued inference job exceeded a bounded pending or running budget."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        job_id: str,
        transport_job_id: str,
    ) -> None:
        self.phase = phase
        self.job_id = job_id
        self.transport_job_id = transport_job_id
        super().__init__(message)


class InferenceTransportFailedError(RuntimeError):
    """A queue transport reached a terminal non-success status."""

    def __init__(self, message: str, *, status: QueueJobStatus) -> None:
        self.status = status
        super().__init__(message)


class RemoteInferenceRejectedError(RuntimeError):
    """Terminal application-level rejection returned through a succeeded transport job."""

    def __init__(
        self,
        job_id: str,
        detail: str,
        *,
        transport_job_id: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.detail = detail
        self.transport_job_id = transport_job_id
        transport = f" transport={transport_job_id}" if transport_job_id else ""
        super().__init__(
            f"Inference job {job_id}{transport} was rejected by worker: {detail}"
        )


def cached_inference_response(
    storage: ObjectStorage,
    request: InferenceJobRequest,
) -> InferenceJobResponse | None:
    """Return a verified cached response using only object-storage metadata reads."""

    stored = storage.stat(request.output.key)
    if stored is None:
        return None

    request_sha256 = request.fingerprint()
    artifact_sha256 = stored.metadata.get("artifact-sha256")
    if (
        stored.metadata.get("job-id") != request.job_id
        or stored.metadata.get("request-sha256") != request_sha256
        or artifact_sha256 is None
    ):
        raise RuntimeError(
            f"Cached inference output metadata does not match request {request.job_id}"
        )
    if stored.content_type != request.output.content_type:
        raise RuntimeError(
            f"Cached inference output content type does not match request {request.job_id}"
        )
    if stored.size_bytes < 1:
        raise RuntimeError(f"Cached inference output is empty for request {request.job_id}")

    for name, contract in (request.sidecar_outputs or {}).items():
        sidecar = storage.stat(contract.key)
        if sidecar is None:
            return None
        sidecar_sha256 = sidecar.metadata.get("artifact-sha256")
        if (
            sidecar.metadata.get("job-id") != request.job_id
            or sidecar.metadata.get("request-sha256") != request_sha256
            or sidecar.metadata.get("sidecar-name") != name
            or sidecar_sha256 is None
        ):
            raise RuntimeError(
                f"Cached inference sidecar metadata does not match request {request.job_id}: "
                f"{name}"
            )
        if sidecar.content_type != contract.content_type:
            raise RuntimeError(
                f"Cached inference sidecar content type does not match request "
                f"{request.job_id}: {name}"
            )
        if sidecar.size_bytes < 1:
            raise RuntimeError(
                f"Cached inference sidecar is empty for request {request.job_id}: {name}"
            )

    return InferenceJobResponse(
        job_id=request.job_id,
        request_sha256=request_sha256,
        output=_artifact_from_stored(stored, artifact_sha256),
        attempt_count=1,
        replayed=True,
    )


class InferenceJobExecutor:
    """Synchronous submit/poll/download client shared by inference providers."""

    def __init__(
        self,
        *,
        queue: JobQueueClient,
        storage: ObjectStorage,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 3600.0,
        pending_timeout_seconds: float | None = None,
    ) -> None:
        if poll_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("poll_seconds and timeout_seconds must be positive")
        if pending_timeout_seconds is not None and pending_timeout_seconds <= 0:
            raise ValueError("pending_timeout_seconds must be positive")
        self._queue = queue
        self._storage = storage
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds
        self._pending_timeout_seconds = (
            timeout_seconds if pending_timeout_seconds is None else pending_timeout_seconds
        )

    @property
    def storage(self) -> ObjectStorage:
        return self._storage

    def ensure_input(
        self,
        source: Path,
        *,
        key: str,
        sha256: str,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        existing = self._storage.stat(key)
        if existing is not None and existing.metadata.get("sha256") == sha256:
            return
        self._storage.upload(
            source,
            key,
            content_type=content_type,
            metadata={"sha256": sha256, **dict(metadata or {})},
        )

    def execute(
        self,
        request: InferenceJobRequest,
        *,
        metadata: Mapping[str, str],
    ) -> InferenceJobResponse:
        cached = cached_inference_response(self._storage, request)
        if cached is not None:
            return cached

        snapshot = self._queue.submit(request, metadata=metadata)
        submitted_at = time.monotonic()
        last_status = snapshot.status
        last_progress_log = submitted_at
        logger.info(
            (
                "Inference transport submitted application_job_id=%s transport_job_id=%s "
                "status=%s metadata=%s"
            ),
            request.job_id,
            snapshot.id,
            snapshot.status.value,
            dict(metadata),
        )
        pending_deadline = submitted_at + self._pending_timeout_seconds
        running_deadline: float | None = None
        last_poll_error: TransientQueueError | None = None

        while snapshot.status in {QueueJobStatus.PENDING, QueueJobStatus.RUNNING}:
            now = time.monotonic()
            if snapshot.status == QueueJobStatus.PENDING:
                if now >= pending_deadline:
                    reconciled = _reconcile_cached_response(self._storage, request)
                    if reconciled is not None:
                        logger.warning(
                            "Inference transport exceeded pending timeout but its R2 artifact "
                            "is complete; replaying application_job_id=%s transport_job_id=%s",
                            request.job_id,
                            snapshot.id,
                        )
                        return reconciled
                    self._raise_timeout(
                        request=request,
                        snapshot_status=snapshot.status,
                        transport_job_id=snapshot.id,
                        last_poll_error=last_poll_error,
                        phase="pending",
                        timeout_seconds=self._pending_timeout_seconds,
                    )
            else:
                if running_deadline is None:
                    running_deadline = now + self._timeout_seconds
                if now >= running_deadline:
                    reconciled = _reconcile_cached_response(self._storage, request)
                    if reconciled is not None:
                        logger.warning(
                            "Inference transport exceeded running timeout but its R2 artifact "
                            "is complete; replaying application_job_id=%s transport_job_id=%s",
                            request.job_id,
                            snapshot.id,
                        )
                        return reconciled
                    self._raise_timeout(
                        request=request,
                        snapshot_status=snapshot.status,
                        transport_job_id=snapshot.id,
                        last_poll_error=last_poll_error,
                        phase="running",
                        timeout_seconds=self._timeout_seconds,
                    )

            time.sleep(self._poll_seconds)
            try:
                snapshot = self._queue.get(snapshot.id)
                last_poll_error = None
                observed_at = time.monotonic()
                if (
                    snapshot.status != last_status
                    or observed_at - last_progress_log >= 30.0
                ):
                    logger.info(
                        (
                            "Inference transport progress application_job_id=%s "
                            "transport_job_id=%s status=%s elapsed_seconds=%.1f"
                        ),
                        request.job_id,
                        snapshot.id,
                        snapshot.status.value,
                        observed_at - submitted_at,
                    )
                    last_status = snapshot.status
                    last_progress_log = observed_at
            except TransientQueueError as exc:
                last_poll_error = exc
                continue

        if snapshot.status != QueueJobStatus.SUCCEEDED:
            raise InferenceTransportFailedError(
                f"Inference job {request.job_id} finished with transport status "
                f"{snapshot.status.value}; transport_job_id={snapshot.id}",
                status=snapshot.status,
            )
        rejection = _terminal_rejection_detail(snapshot.output)
        if rejection is not None:
            logger.warning(
                "Inference worker rejection application_job_id=%s transport_job_id=%s detail=%s",
                request.job_id,
                snapshot.id,
                rejection,
            )
            raise RemoteInferenceRejectedError(
                request.job_id,
                rejection,
                transport_job_id=snapshot.id,
            )

        response = InferenceJobResponse.model_validate(snapshot.output)
        if response.job_id != request.job_id:
            raise RuntimeError("Inference response job_id does not match the submitted request")
        if response.request_sha256 != request.fingerprint():
            raise RuntimeError(
                "Inference response fingerprint does not match the submitted request"
            )
        return response

    def _raise_timeout(
        self,
        *,
        request: InferenceJobRequest,
        snapshot_status: QueueJobStatus,
        transport_job_id: str,
        last_poll_error: TransientQueueError | None,
        phase: str,
        timeout_seconds: float,
    ) -> None:
        message = (
            f"Inference job {request.job_id} exceeded the {phase} timeout of "
            f"{timeout_seconds} seconds"
        )
        cancellation_error: Exception | None = None
        if snapshot_status == QueueJobStatus.PENDING:
            try:
                self._queue.cancel(transport_job_id)
                message += f"; cancelled pending transport job {transport_job_id}"
            except Exception as exc:
                cancellation_error = exc
                message += (
                    f"; failed to cancel pending transport job {transport_job_id}: {exc}"
                )
        elif snapshot_status == QueueJobStatus.RUNNING:
            message += (
                f"; transport job {transport_job_id} was already dispatched and was not cancelled"
            )

        if last_poll_error is not None:
            message += f"; last queue polling error: {last_poll_error}"
            raise InferenceJobTimeoutError(
                message,
                phase=phase,
                job_id=request.job_id,
                transport_job_id=transport_job_id,
            ) from last_poll_error
        if cancellation_error is not None:
            raise InferenceJobTimeoutError(
                message,
                phase=phase,
                job_id=request.job_id,
                transport_job_id=transport_job_id,
            ) from cancellation_error
        raise InferenceJobTimeoutError(
            message,
            phase=phase,
            job_id=request.job_id,
            transport_job_id=transport_job_id,
        )

    def download_output(self, response: InferenceJobResponse, destination: Path) -> None:
        stored = self._storage.download(response.output.key, destination)
        if stored.size_bytes != response.output.size_bytes:
            raise RuntimeError("Downloaded inference artifact size does not match worker response")
        digest = sha256_file(destination)
        if digest != response.output.sha256:
            raise RuntimeError(
                "Downloaded inference artifact SHA-256 does not match worker response"
            )


def _terminal_rejection_detail(output: Any) -> str | None:
    if not isinstance(output, Mapping):
        return None
    if set(output) != {"detail"}:
        return None
    detail = output.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        return None
    return detail.strip()


def _reconcile_cached_response(
    storage: ObjectStorage,
    request: InferenceJobRequest,
) -> InferenceJobResponse | None:
    """Recover a completed R2 artifact when transport state is stale at a deadline."""

    try:
        return cached_inference_response(storage, request)
    except RuntimeError as exc:
        logger.warning(
            "Inference timeout cache reconciliation rejected application_job_id=%s: %s",
            request.job_id,
            exc,
        )
        return None


def _artifact_from_stored(stored: StoredObject, sha256: str) -> OutputArtifact:
    return OutputArtifact(
        key=stored.key,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        sha256=sha256,
        etag=stored.etag,
    )
