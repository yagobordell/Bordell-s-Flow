from __future__ import annotations

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


class RemoteInferenceRejectedError(RuntimeError):
    """Terminal application-level rejection returned through a succeeded transport job."""

    def __init__(self, job_id: str, detail: str) -> None:
        self.job_id = job_id
        self.detail = detail
        super().__init__(f"Inference job {job_id} was rejected by worker: {detail}")


class InferenceJobExecutor:
    """Synchronous submit/poll/download client shared by Salad-backed providers."""

    def __init__(
        self,
        *,
        queue: JobQueueClient,
        storage: ObjectStorage,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 3600.0,
    ) -> None:
        if poll_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("poll_seconds and timeout_seconds must be positive")
        self._queue = queue
        self._storage = storage
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds

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
        cached = self._cached_response(request)
        if cached is not None:
            return cached

        snapshot = self._queue.submit(request, metadata=metadata)
        deadline = time.monotonic() + self._timeout_seconds
        last_poll_error: TransientQueueError | None = None

        while snapshot.status in {QueueJobStatus.PENDING, QueueJobStatus.RUNNING}:
            if time.monotonic() >= deadline:
                message = (
                    f"Inference job {request.job_id} did not finish within "
                    f"{self._timeout_seconds} seconds"
                )
                if last_poll_error is not None:
                    message += f"; last queue polling error: {last_poll_error}"
                    raise TimeoutError(message) from last_poll_error
                raise TimeoutError(message)

            time.sleep(self._poll_seconds)
            try:
                snapshot = self._queue.get(snapshot.id)
                last_poll_error = None
            except TransientQueueError as exc:
                last_poll_error = exc
                continue

        if snapshot.status != QueueJobStatus.SUCCEEDED:
            raise RuntimeError(
                f"Inference job {request.job_id} finished with transport status "
                f"{snapshot.status.value}"
            )
        rejection = _terminal_rejection_detail(snapshot.output)
        if rejection is not None:
            raise RemoteInferenceRejectedError(request.job_id, rejection)

        response = InferenceJobResponse.model_validate(snapshot.output)
        if response.job_id != request.job_id:
            raise RuntimeError("Inference response job_id does not match the submitted request")
        if response.request_sha256 != request.fingerprint():
            raise RuntimeError(
                "Inference response fingerprint does not match the submitted request"
            )
        return response

    def _cached_response(self, request: InferenceJobRequest) -> InferenceJobResponse | None:
        stored = self._storage.stat(request.output.key)
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

        return InferenceJobResponse(
            job_id=request.job_id,
            request_sha256=request_sha256,
            output=_artifact_from_stored(stored, artifact_sha256),
            attempt_count=1,
            replayed=True,
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


def _artifact_from_stored(stored: StoredObject, sha256: str) -> OutputArtifact:
    return OutputArtifact(
        key=stored.key,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        sha256=sha256,
        etag=stored.etag,
    )
