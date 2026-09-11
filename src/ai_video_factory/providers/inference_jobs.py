from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest, InferenceJobResponse
from ai_video_factory.inference.ports import ObjectStorage
from ai_video_factory.inference.storage import sha256_file

from .job_queue import JobQueueClient, QueueJobStatus


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
        snapshot = self._queue.submit(request, metadata=metadata)
        deadline = time.monotonic() + self._timeout_seconds

        while snapshot.status in {QueueJobStatus.PENDING, QueueJobStatus.RUNNING}:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Inference job {request.job_id} did not finish within "
                    f"{self._timeout_seconds} seconds"
                )
            time.sleep(self._poll_seconds)
            snapshot = self._queue.get(snapshot.id)

        if snapshot.status != QueueJobStatus.SUCCEEDED:
            raise RuntimeError(
                f"Inference job {request.job_id} finished with transport status "
                f"{snapshot.status.value}"
            )
        response = InferenceJobResponse.model_validate(snapshot.output)
        if response.job_id != request.job_id:
            raise RuntimeError("Inference response job_id does not match the submitted request")
        if response.request_sha256 != request.fingerprint():
            raise RuntimeError("Inference response fingerprint does not match the submitted request")
        return response

    def download_output(self, response: InferenceJobResponse, destination: Path) -> None:
        stored = self._storage.download(response.output.key, destination)
        if stored.size_bytes != response.output.size_bytes:
            raise RuntimeError("Downloaded inference artifact size does not match worker response")
        digest = sha256_file(destination)
        if digest != response.output.sha256:
            raise RuntimeError("Downloaded inference artifact SHA-256 does not match worker response")
