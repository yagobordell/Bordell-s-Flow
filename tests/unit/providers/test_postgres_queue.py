from datetime import UTC, datetime, timedelta

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.providers.job_queue import QueueJobStatus
from ai_video_factory.providers.postgres_queue import (
    PostgresJobQueueClient,
    _can_rebind_legacy_run_metadata_failure,
    _legacy_run_metadata_fingerprint,
    _run_metadata,
)


def _row(status: str, **extra):
    return {
        "job_id": "job-1",
        "request_sha256": "a" * 64,
        "status": status,
        "attempt_count": 2,
        "lease_expires_at": None,
        "result": None,
        "last_error": None,
        **extra,
    }


def test_snapshot_maps_canonical_postgres_states() -> None:
    assert PostgresJobQueueClient._snapshot(_row("pending")).status is QueueJobStatus.PENDING
    assert (
        PostgresJobQueueClient._snapshot(_row("retryable_failed")).status
        is QueueJobStatus.PENDING
    )
    assert PostgresJobQueueClient._snapshot(_row("running")).status is QueueJobStatus.RUNNING
    assert PostgresJobQueueClient._snapshot(_row("succeeded")).status is QueueJobStatus.SUCCEEDED
    assert PostgresJobQueueClient._snapshot(_row("failed")).status is QueueJobStatus.FAILED
    assert PostgresJobQueueClient._snapshot(_row("cancelled")).status is QueueJobStatus.CANCELLED


def test_expired_running_lease_is_observed_as_pending() -> None:
    snapshot = PostgresJobQueueClient._snapshot(
        _row("running", lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert snapshot.status is QueueJobStatus.PENDING


def test_active_running_lease_stays_running() -> None:
    snapshot = PostgresJobQueueClient._snapshot(
        _row("running", lease_expires_at=datetime.now(UTC) + timedelta(seconds=60))
    )
    assert snapshot.status is QueueJobStatus.RUNNING


def test_run_metadata_is_correlation_only_and_legacy_count_is_ignored() -> None:
    assert _run_metadata({}) is None
    assert _run_metadata({"run_id": "abc123"}) == "abc123"
    assert _run_metadata({"run_id": "abc123", "run_job_count": "5"}) == "abc123"


def test_only_exact_legacy_validation_failure_can_rebind() -> None:
    request = InferenceJobRequest(
        job_id="ltx-a2v-b2-1A-deadbeef",
        task="video.ltx25.audio_to_video",
        output=ObjectOutput(
            key="jobs/ltx-a2v-b2-1A-deadbeef/output.mp4",
            content_type="video/mp4",
        ),
        parameters={"prompt": "A Person speaking to the camera."},
    )
    legacy_sha = _legacy_run_metadata_fingerprint(
        request, run_id="901d3792b69861844519", run_job_count=5
    )
    row = {
        "status": "failed",
        "request_sha256": legacy_sha,
        "run_id": "901d3792b69861844519",
        "run_job_count": 5,
        "last_error": (
            "ValidationError: run_id Extra inputs are not permitted [type=extra_forbidden]; "
            "run_job_count Extra inputs are not permitted [type=extra_forbidden]"
        ),
    }

    assert _can_rebind_legacy_run_metadata_failure(
        row,
        request,
        run_id="901d3792b69861844519",
    ) is True
    assert _can_rebind_legacy_run_metadata_failure(
        {**row, "last_error": "CUDA out of memory"},
        request,
        run_id="901d3792b69861844519",
    ) is False
