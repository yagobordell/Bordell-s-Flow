from datetime import UTC, datetime, timedelta

from ai_video_factory.providers.job_queue import QueueJobStatus
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient


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
    assert PostgresJobQueueClient._snapshot(_row("retryable_failed")).status is QueueJobStatus.PENDING
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
