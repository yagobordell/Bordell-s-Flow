from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_video_factory.inference.bundle_publication import (
    publish_committed_bundle,
    stage_bundle,
)
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file
from ai_video_factory.providers.inference_jobs import (
    IncompleteInferenceBundleError,
    InferenceJobExecutor,
    InferenceJobTimeoutError,
    InferenceQueueAuthorityError,
    RemoteInferenceRejectedError,
)
from ai_video_factory.providers.job_queue import (
    QueueJobSnapshot,
    QueueJobStatus,
    TransientQueueError,
)


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


class ArtifactAppearsAfterSubmitStorage(FakeStorage):
    def __init__(self, stored: StoredObject) -> None:
        super().__init__(stored)
        self.stat_calls = 0

    def stat(self, key: str) -> StoredObject | None:
        self.stat_calls += 1
        if self.stat_calls == 1:
            return None
        return super().stat(key)


class FakeQueue:
    def __init__(self, snapshot: QueueJobSnapshot) -> None:
        self.snapshot = snapshot
        self.submits = 0
        self.cancellations: list[str] = []

    def submit(self, request: InferenceJobRequest, *, metadata: dict[str, str]) -> QueueJobSnapshot:
        self.submits += 1
        return self.snapshot

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        return self.snapshot

    def cancel(self, transport_job_id: str) -> None:
        self.cancellations.append(transport_job_id)


class AuthorityFailureQueue(FakeQueue):
    def reconcile_recovered_success(self, request, response):
        del request, response
        raise RuntimeError("Postgres unavailable")


class TransientThenSuccessQueue:
    def __init__(self, failures: int = 2) -> None:
        self.failures = failures
        self.submits = 0
        self.gets = 0
        self.request: InferenceJobRequest | None = None

    def submit(self, request: InferenceJobRequest, *, metadata: dict[str, str]) -> QueueJobSnapshot:
        self.submits += 1
        self.request = request
        return QueueJobSnapshot(id="transport-001", status=QueueJobStatus.PENDING)

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        self.gets += 1
        if self.gets <= self.failures:
            raise TransientQueueError("temporary Salad read timeout")
        assert self.request is not None
        return _success_snapshot(self.request, transport_job_id)

    def cancel(self, transport_job_id: str) -> None:
        raise AssertionError("successful polling path must not cancel transport jobs")


class PendingThenRunningSuccessQueue:
    def __init__(self) -> None:
        self.request: InferenceJobRequest | None = None
        self.gets = 0

    def submit(self, request: InferenceJobRequest, *, metadata: dict[str, str]) -> QueueJobSnapshot:
        self.request = request
        return QueueJobSnapshot(id="transport-cold-start", status=QueueJobStatus.PENDING)

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        self.gets += 1
        if self.gets == 1:
            return QueueJobSnapshot(id=transport_job_id, status=QueueJobStatus.PENDING)
        if self.gets == 2:
            return QueueJobSnapshot(id=transport_job_id, status=QueueJobStatus.RUNNING)
        assert self.request is not None
        return _success_snapshot(self.request, transport_job_id)

    def cancel(self, transport_job_id: str) -> None:
        raise AssertionError("successful cold-start path must not cancel transport jobs")


def _request() -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id="image-test-001",
        task="image.test",
        output=ObjectOutput(
            key="jobs/image-test-001/image.png",
            content_type="image/png",
        ),
    )


def _stored_for_request(request: InferenceJobRequest) -> StoredObject:
    return StoredObject(
        key=request.output.key,
        content_type=request.output.content_type,
        size_bytes=123,
        etag="etag-1",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": "a" * 64,
        },
    )


def _success_snapshot(
    request: InferenceJobRequest,
    transport_job_id: str,
) -> QueueJobSnapshot:
    return QueueJobSnapshot(
        id=transport_job_id,
        status=QueueJobStatus.SUCCEEDED,
        output={
            "schema_version": "1",
            "job_id": request.job_id,
            "status": "succeeded",
            "request_sha256": request.fingerprint(),
            "output": {
                "key": request.output.key,
                "content_type": request.output.content_type,
                "size_bytes": 123,
                "sha256": "a" * 64,
                "etag": "etag-1",
            },
            "attempt_count": 1,
            "replayed": False,
        },
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
    assert queue.cancellations == []


def test_executor_fails_closed_when_cache_authority_is_unavailable() -> None:
    request = _request()
    stored = _stored_for_request(request)
    queue = AuthorityFailureQueue(
        QueueJobSnapshot(
            id="authority-unavailable",
            status=QueueJobStatus.PENDING,
        )
    )
    executor = InferenceJobExecutor(
        queue=queue,  # type: ignore[arg-type]
        storage=FakeStorage(stored),
        poll_seconds=0.01,
        timeout_seconds=1,
    )

    with pytest.raises(
        InferenceQueueAuthorityError,
        match="authoritative queue state is unavailable",
    ):
        executor.execute(request, metadata={"phase": "4"})

    assert queue.submits == 0


def test_executor_keeps_polling_after_transient_queue_read_failures() -> None:
    request = _request()
    queue = TransientThenSuccessQueue(failures=2)
    executor = InferenceJobExecutor(
        queue=queue,  # type: ignore[arg-type]
        storage=ArtifactAppearsAfterSubmitStorage(_stored_for_request(request)),
        poll_seconds=0.001,
        timeout_seconds=1,
    )

    response = executor.execute(request, metadata={"phase": "4"})

    assert response.status == "succeeded"
    assert queue.submits == 1
    assert queue.gets == 3


def test_pending_wait_does_not_consume_running_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "ai_video_factory.providers.inference_jobs.time.monotonic",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        "ai_video_factory.providers.inference_jobs.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    request = _request()
    queue = PendingThenRunningSuccessQueue()
    executor = InferenceJobExecutor(
        queue=queue,  # type: ignore[arg-type]
        storage=ArtifactAppearsAfterSubmitStorage(_stored_for_request(request)),
        poll_seconds=4,
        timeout_seconds=6,
        pending_timeout_seconds=20,
    )

    response = executor.execute(request, metadata={"phase": "4"})

    assert response.status == "succeeded"
    assert clock["now"] == 12
    assert queue.gets == 3


def test_executor_cancels_pending_transport_when_pending_timeout_expires() -> None:
    queue = FakeQueue(
        QueueJobSnapshot(
            id="transport-pending",
            status=QueueJobStatus.PENDING,
        )
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=FakeStorage(),
        poll_seconds=0.001,
        timeout_seconds=1,
        pending_timeout_seconds=0.003,
    )

    with pytest.raises(TimeoutError, match="cancelled pending transport job transport-pending"):
        executor.execute(_request(), metadata={"phase": "4"})

    assert queue.submits == 1
    assert queue.cancellations == ["transport-pending"]


def test_executor_does_not_cancel_transport_after_running_timeout() -> None:
    queue = FakeQueue(
        QueueJobSnapshot(
            id="transport-running",
            status=QueueJobStatus.RUNNING,
        )
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=FakeStorage(),
        poll_seconds=0.001,
        timeout_seconds=0.003,
        pending_timeout_seconds=1,
    )

    with pytest.raises(
        InferenceJobTimeoutError,
        match="already dispatched and was not cancelled",
    ) as captured:
        executor.execute(_request(), metadata={"phase": "4"})

    assert captured.value.phase == "running"
    assert captured.value.job_id == "image-test-001"
    assert captured.value.transport_job_id == "transport-running"
    assert queue.submits == 1
    assert queue.cancellations == []


def test_executor_timeout_cache_fails_closed_when_authority_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "ai_video_factory.providers.inference_jobs.time.monotonic",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        "ai_video_factory.providers.inference_jobs.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    request = _request()
    queue = AuthorityFailureQueue(
        QueueJobSnapshot(
            id="authority-timeout",
            status=QueueJobStatus.RUNNING,
        )
    )
    executor = InferenceJobExecutor(
        queue=queue,  # type: ignore[arg-type]
        storage=ArtifactAppearsAfterSubmitStorage(_stored_for_request(request)),
        poll_seconds=0.01,
        timeout_seconds=0.01,
        pending_timeout_seconds=1,
    )

    with pytest.raises(
        InferenceQueueAuthorityError,
        match="authoritative queue state is unavailable",
    ):
        executor.execute(request, metadata={"phase": "6"})

    assert queue.submits == 1


def test_executor_reconciles_completed_r2_artifact_at_running_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "ai_video_factory.providers.inference_jobs.time.monotonic",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        "ai_video_factory.providers.inference_jobs.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    request = _request()
    stored = StoredObject(
        key=request.output.key,
        content_type=request.output.content_type,
        size_bytes=123,
        etag="etag-complete",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": "a" * 64,
        },
    )
    storage = ArtifactAppearsAfterSubmitStorage(stored)
    queue = FakeQueue(
        QueueJobSnapshot(
            id="transport-stale-running",
            status=QueueJobStatus.RUNNING,
        )
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=0.01,
        timeout_seconds=0.01,
        pending_timeout_seconds=1,
    )

    response = executor.execute(request, metadata={"phase": "6"})

    assert response.job_id == request.job_id
    assert response.replayed is True
    assert storage.stat_calls == 2
    assert queue.cancellations == []


def _sidecar_request(job_id: str) -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id=job_id,
        task="test.sidecar",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.mp4",
            content_type="video/mp4",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
    )


def _succeeded_snapshot_for_stored(
    request: InferenceJobRequest,
    stored: StoredObject,
) -> QueueJobSnapshot:
    return QueueJobSnapshot(
        id=request.job_id,
        status=QueueJobStatus.SUCCEEDED,
        output={
            "schema_version": "1",
            "job_id": request.job_id,
            "status": "succeeded",
            "request_sha256": request.fingerprint(),
            "output": {
                "key": stored.key,
                "content_type": stored.content_type,
                "size_bytes": stored.size_bytes,
                "sha256": stored.metadata["artifact-sha256"],
                "etag": stored.etag,
            },
            "attempt_count": 1,
            "replayed": False,
        },
    )


def test_executor_recovers_missing_sidecar_from_committed_bundle(tmp_path: Path) -> None:
    request = _sidecar_request("bundle-replay-recover")
    storage = LocalObjectStorage(tmp_path / "objects")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    primary = work_dir / "output.mp4"
    sidecar = work_dir / "metadata.json"
    primary.write_bytes(b"video-bytes")
    sidecar.write_text('{"ok":true}\n', encoding="utf-8")

    committed = stage_bundle(
        storage,
        request,
        request.fingerprint(),
        primary_path=primary,
        primary_content_type="video/mp4",
        sidecars={"metadata": (sidecar, "application/json")},
        work_dir=work_dir,
    )
    stored_primary, _ = publish_committed_bundle(
        storage,
        request,
        request.fingerprint(),
        committed,
        work_dir,
        local_sources={
            "primary": primary,
            "sidecar:metadata": sidecar,
        },
    )
    storage._path(request.sidecar_outputs["metadata"].key).unlink()

    queue = FakeQueue(_succeeded_snapshot_for_stored(request, stored_primary))
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=0.01,
        timeout_seconds=1,
    )

    response = executor.execute(request, metadata={"phase": "8"})

    assert response.output.sha256 == sha256_file(primary)
    assert storage.stat(request.sidecar_outputs["metadata"].key) is not None
    assert queue.submits == 1


def test_executor_rejects_succeeded_postgres_job_with_unrecoverable_missing_sidecar(
    tmp_path: Path,
) -> None:
    request = _sidecar_request("legacy-incomplete-replay")
    storage = LocalObjectStorage(tmp_path / "objects")
    primary = tmp_path / "legacy.mp4"
    primary.write_bytes(b"legacy-video")
    digest = sha256_file(primary)
    stored = storage.upload(
        primary,
        request.output.key,
        content_type="video/mp4",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": digest,
        },
    )
    queue = FakeQueue(_succeeded_snapshot_for_stored(request, stored))
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=0.01,
        timeout_seconds=1,
    )

    with pytest.raises(IncompleteInferenceBundleError, match="artifact bundle is incomplete"):
        executor.execute(request, metadata={"phase": "8"})

    assert queue.submits == 1
