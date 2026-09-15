from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.inference_jobs import (
    InferenceJobExecutor,
    RemoteInferenceRejectedError,
)
from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus


class FakeStorage:
    def __init__(self, stored: StoredObject | None = None) -> None:
        self.stored = stored

    def stat(self, key: str) -> StoredObject | None:
        if self.stored is not None and self.stored.key == key:
            return self.stored
        return None

    def download(self, key: str, destination: Path) -> StoredObject:
        raise AssertionError("download was not expected")

    def upload(self, *args: Any, **kwargs: Any) -> StoredObject:
        raise AssertionError("upload was not expected")

    def ping(self) -> None:
        return None


class FakeQueue:
    def __init__(self, snapshot: QueueJobSnapshot) -> None:
        self.snapshot = snapshot
        self.submits = 0

    def submit(self, request: InferenceJobRequest, *, metadata: dict[str, str]) -> QueueJobSnapshot:
        self.submits += 1
        return self.snapshot

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        return self.snapshot


def _request() -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id="image-test-001",
        task="image.test",
        output=ObjectOutput(
            key="jobs/image-test-001/image.png",
            content_type="image/png",
        ),
    )


def test_executor_surfaces_terminal_worker_detail_without_pydantic_validation_error() -> None:
    queue = FakeQueue(
        QueueJobSnapshot(
            id="transport-001",
            status=QueueJobStatus.SUCCEEDED,
            output={
                "detail": (
                    "Ideogram 4 safety filter blocked all deterministic caption variants"
                )
            },
        )
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=FakeStorage(),
        poll_seconds=0.01,
        timeout_seconds=1,
    )

    with pytest.raises(RemoteInferenceRejectedError) as captured:
        executor.execute(_request(), metadata={"phase": "4"})

    assert captured.value.detail == (
        "Ideogram 4 safety filter blocked all deterministic caption variants"
    )
    assert queue.submits == 1


def test_executor_reuses_verified_object_storage_artifact_without_queue_submission() -> None:
    request = _request()
    digest = "a" * 64
    stored = StoredObject(
        key=request.output.key,
        content_type="image/png",
        size_bytes=123,
        etag="etag-1",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": digest,
        },
    )
    queue = FakeQueue(
        QueueJobSnapshot(
            id="unused",
            status=QueueJobStatus.PENDING,
        )
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=FakeStorage(stored),
        poll_seconds=0.01,
        timeout_seconds=1,
    )

    response = executor.execute(request, metadata={"phase": "4"})

    assert response.job_id == request.job_id
    assert response.output.sha256 == digest
    assert response.output.size_bytes == 123
    assert response.replayed is True
    assert queue.submits == 0
