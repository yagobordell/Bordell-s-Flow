from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from ai_video_factory.inference.bundle_publication import (
    bundle_manifest_key,
    publish_committed_bundle,
    stage_bundle,
)
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file
from ai_video_factory.providers.inference_jobs import (
    InferenceJobExecutor,
    InferenceTransportFailedError,
    cached_inference_response,
)
from ai_video_factory.providers.job_queue import QueueJobStatus
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient


def _postgres_dsn() -> str:
    dsn = os.getenv("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    return dsn


class CacheAppearsOnTimeoutStorage(LocalObjectStorage):
    def __init__(self, root: Path, *, on_reveal) -> None:
        super().__init__(root)
        self._on_reveal = on_reveal
        self._armed = False
        self._stat_calls = 0

    def arm(self) -> None:
        self._armed = True
        self._stat_calls = 0

    def stat(self, key: str):
        if not self._armed:
            return super().stat(key)
        self._stat_calls += 1
        if self._stat_calls == 1:
            return None
        if self._stat_calls == 2:
            self._on_reveal()
        return super().stat(key)


@pytest.mark.parametrize("terminal_status", ["pending", "retryable_failed", "failed"])
def test_committed_bundle_recovers_non_active_postgres_without_new_attempt(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    dsn = _postgres_dsn()
    job_id = f"terminal-bundle-{terminal_status}"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.terminal_bundle_recovery",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.bin",
            content_type="application/octet-stream",
        ),
        max_attempts=5,
    )
    storage = LocalObjectStorage(tmp_path / terminal_status / "objects")
    work_dir = tmp_path / terminal_status / "work"
    work_dir.mkdir(parents=True)
    primary = work_dir / "output.bin"
    primary.write_bytes(b"recoverable-generated-bytes\n")

    stage_bundle(
        storage,
        request,
        request.fingerprint(),
        primary_path=primary,
        primary_content_type=request.output.content_type,
        sidecars={},
        work_dir=work_dir,
    )
    assert storage.stat(request.output.key) is None
    assert storage.stat(bundle_manifest_key(request, request.fingerprint())) is not None

    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={"test": "terminal-recovery"})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = %s,
                    attempt_count = 5,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    result = NULL,
                    last_error = 'simulated repeated final R2 publication failure',
                    updated_at = now()
                WHERE job_id = %s
                """,
                (terminal_status, request.job_id),
            )

        executor = InferenceJobExecutor(
            queue=queue,
            storage=storage,
            poll_seconds=0.01,
            timeout_seconds=1,
        )
        response = executor.execute(request, metadata={"phase": "terminal-recovery"})

        assert response.replayed is True
        assert response.attempt_count == 5
        assert response.output.sha256 == sha256_file(primary)
        assert storage.stat(request.output.key) is not None

        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, result, last_error
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (request.job_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "succeeded"
        assert row[1] == 5
        assert row[2] is not None
        assert row[3] is None
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (request.job_id,))


def test_expired_running_lease_recovers_committed_bundle_without_new_attempt(
    tmp_path: Path,
) -> None:
    dsn = _postgres_dsn()
    job_id = "expired-running-bundle-recovery"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.expired_running_bundle_recovery",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.bin",
            content_type="application/octet-stream",
        ),
        max_attempts=5,
    )
    storage = LocalObjectStorage(tmp_path / "expired-running" / "objects")
    work_dir = tmp_path / "expired-running" / "work"
    work_dir.mkdir(parents=True)
    primary = work_dir / "output.bin"
    primary.write_bytes(b"generated-before-lease-expired\n")

    stage_bundle(
        storage,
        request,
        request.fingerprint(),
        primary_path=primary,
        primary_content_type=request.output.content_type,
        sidecars={},
        work_dir=work_dir,
    )

    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={"test": "expired-running-recovery"})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'running',
                    attempt_count = 3,
                    lease_owner = 'expired-worker',
                    lease_expires_at = now() + interval '60 seconds',
                    result = NULL,
                    last_error = NULL,
                    updated_at = now()
                WHERE job_id = %s
                """,
                (request.job_id,),
            )

        class ExpireLeaseAfterSubmitQueue:
            def submit(self, submitted_request, *, metadata):
                snapshot = queue.submit(submitted_request, metadata=metadata)
                with psycopg.connect(dsn, autocommit=True) as connection:
                    connection.execute(
                        """
                        UPDATE gpu.jobs
                        SET lease_expires_at = now() - interval '1 second',
                            updated_at = now()
                        WHERE job_id = %s
                        """,
                        (submitted_request.job_id,),
                    )
                return snapshot

            def get(self, transport_job_id):
                return queue.get(transport_job_id)

            def cancel(self, transport_job_id):
                return queue.cancel(transport_job_id)

            def reconcile_recovered_success(self, submitted_request, response):
                return queue.reconcile_recovered_success(submitted_request, response)

        executor = InferenceJobExecutor(
            queue=ExpireLeaseAfterSubmitQueue(),
            storage=storage,
            poll_seconds=0.01,
            timeout_seconds=1,
            pending_timeout_seconds=1,
        )
        response = executor.execute(request, metadata={"phase": "expired-running-recovery"})

        assert response.replayed is True
        assert response.attempt_count == 3
        assert response.output.sha256 == sha256_file(primary)

        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, result, lease_owner, lease_expires_at
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (request.job_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "succeeded"
        assert row[1] == 3
        assert row[2] is not None
        assert row[3] is None
        assert row[4] is None
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (request.job_id,))


def test_verified_final_cache_reconciles_stale_failed_postgres_state(
    tmp_path: Path,
) -> None:
    dsn = _postgres_dsn()
    job_id = "terminal-bundle-final-cache"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.terminal_bundle_final_cache",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.bin",
            content_type="application/octet-stream",
        ),
        max_attempts=5,
    )
    storage = LocalObjectStorage(tmp_path / "final-cache" / "objects")
    work_dir = tmp_path / "final-cache" / "work"
    work_dir.mkdir(parents=True)
    primary = work_dir / "output.bin"
    primary.write_bytes(b"already-published-generated-bytes\n")

    committed = stage_bundle(
        storage,
        request,
        request.fingerprint(),
        primary_path=primary,
        primary_content_type=request.output.content_type,
        sidecars={},
        work_dir=work_dir,
    )
    publish_committed_bundle(
        storage,
        request,
        request.fingerprint(),
        committed,
        work_dir,
        local_sources={"primary": primary},
    )

    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={"test": "terminal-cache-recovery"})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'failed',
                    attempt_count = 5,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    result = NULL,
                    last_error = 'simulated Postgres reconciliation outage',
                    updated_at = now()
                WHERE job_id = %s
                """,
                (request.job_id,),
            )

        executor = InferenceJobExecutor(
            queue=queue,
            storage=storage,
            poll_seconds=0.01,
            timeout_seconds=1,
        )
        response = executor.execute(request, metadata={"phase": "terminal-cache-recovery"})

        assert response.replayed is True
        assert response.attempt_count == 5
        assert response.output.sha256 == sha256_file(primary)

        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, result, last_error
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (request.job_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "succeeded"
        assert row[1] == 5
        assert row[2] is not None
        assert row[3] is None
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (request.job_id,))


@pytest.mark.parametrize("protected_status", ["running", "cancelled"])
def test_recovery_only_path_never_overwrites_active_or_cancelled_state(
    tmp_path: Path,
    protected_status: str,
) -> None:
    dsn = _postgres_dsn()
    job_id = f"bundle-recovery-protected-{protected_status}"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.bundle_recovery_protected",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.bin",
            content_type="application/octet-stream",
        ),
    )
    storage = LocalObjectStorage(tmp_path / protected_status / "objects")
    work_dir = tmp_path / protected_status / "work"
    work_dir.mkdir(parents=True)
    primary = work_dir / "output.bin"
    primary.write_bytes(b"verified-final-bytes\n")

    committed = stage_bundle(
        storage,
        request,
        request.fingerprint(),
        primary_path=primary,
        primary_content_type=request.output.content_type,
        sidecars={},
        work_dir=work_dir,
    )
    publish_committed_bundle(
        storage,
        request,
        request.fingerprint(),
        committed,
        work_dir,
        local_sources={"primary": primary},
    )
    response = cached_inference_response(storage, request)
    assert response is not None

    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={"test": "protected-recovery-state"})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = %s,
                    attempt_count = 1,
                    lease_owner = CASE WHEN %s = 'running' THEN 'active-worker' ELSE NULL END,
                    lease_expires_at = CASE
                        WHEN %s = 'running' THEN now() + interval '60 seconds'
                        ELSE NULL
                    END,
                    updated_at = now()
                WHERE job_id = %s
                """,
                (
                    protected_status,
                    protected_status,
                    protected_status,
                    request.job_id,
                ),
            )

        with pytest.raises(RuntimeError, match="cannot reconcile recovered bundle"):
            queue.reconcile_recovered_success(request, response)

        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT status, attempt_count FROM gpu.jobs WHERE job_id = %s",
                (request.job_id,),
            ).fetchone()
        assert row == (protected_status, 1)
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (request.job_id,))


def test_executor_cache_hit_respects_cancelled_postgres_state(tmp_path: Path) -> None:
    dsn = _postgres_dsn()
    job_id = "cancelled-cache-authority"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.cancelled_cache_authority",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.bin",
            content_type="application/octet-stream",
        ),
    )
    storage = LocalObjectStorage(tmp_path / "cancelled-cache" / "objects")
    work_dir = tmp_path / "cancelled-cache" / "work"
    work_dir.mkdir(parents=True)
    primary = work_dir / "output.bin"
    primary.write_bytes(b"complete-but-cancelled\n")

    committed = stage_bundle(
        storage,
        request,
        request.fingerprint(),
        primary_path=primary,
        primary_content_type=request.output.content_type,
        sidecars={},
        work_dir=work_dir,
    )
    publish_committed_bundle(
        storage,
        request,
        request.fingerprint(),
        committed,
        work_dir,
        local_sources={"primary": primary},
    )

    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={"test": "cancelled-cache-authority"})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'cancelled',
                    attempt_count = 1,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE job_id = %s
                """,
                (request.job_id,),
            )

        executor = InferenceJobExecutor(
            queue=queue,
            storage=storage,
            poll_seconds=0.01,
            timeout_seconds=1,
        )
        with pytest.raises(InferenceTransportFailedError) as captured:
            executor.execute(request, metadata={"phase": "cancelled-cache"})

        assert captured.value.status is QueueJobStatus.CANCELLED
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT status, attempt_count FROM gpu.jobs WHERE job_id = %s",
                (request.job_id,),
            ).fetchone()
        assert row == ("cancelled", 1)
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (request.job_id,))


def test_timeout_cache_replay_respects_cancellation_that_happens_while_waiting(
    tmp_path: Path,
) -> None:
    dsn = _postgres_dsn()
    job_id = "cancelled-during-timeout-reconcile"
    request = InferenceJobRequest(
        job_id=job_id,
        task="test.cancelled_during_timeout",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.bin",
            content_type="application/octet-stream",
        ),
    )

    def cancel_job() -> None:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'cancelled',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE job_id = %s
                """,
                (request.job_id,),
            )

    storage = CacheAppearsOnTimeoutStorage(
        tmp_path / "cancel-during-timeout" / "objects",
        on_reveal=cancel_job,
    )
    work_dir = tmp_path / "cancel-during-timeout" / "work"
    work_dir.mkdir(parents=True)
    primary = work_dir / "output.bin"
    primary.write_bytes(b"completed-before-cancellation\n")
    committed = stage_bundle(
        storage,
        request,
        request.fingerprint(),
        primary_path=primary,
        primary_content_type=request.output.content_type,
        sidecars={},
        work_dir=work_dir,
    )
    publish_committed_bundle(
        storage,
        request,
        request.fingerprint(),
        committed,
        work_dir,
        local_sources={"primary": primary},
    )

    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        storage.arm()
        executor = InferenceJobExecutor(
            queue=queue,
            storage=storage,
            poll_seconds=0.01,
            timeout_seconds=0.01,
            pending_timeout_seconds=0.01,
        )

        with pytest.raises(InferenceTransportFailedError) as captured:
            executor.execute(request, metadata={"phase": "cancel-during-timeout"})

        assert captured.value.status is QueueJobStatus.CANCELLED
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT status FROM gpu.jobs WHERE job_id = %s",
                (request.job_id,),
            ).fetchone()
        assert row == ("cancelled",)
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (request.job_id,))
