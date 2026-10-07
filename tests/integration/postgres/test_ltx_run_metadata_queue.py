from __future__ import annotations

import os

import psycopg
import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.repository import PostgresJobRepository
from ai_video_factory.providers.job_queue import QueueJobStatus
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient


def _postgres_dsn() -> str:
    dsn = os.getenv("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    return dsn


def test_legacy_ltx_run_metadata_failure_rebinds_without_model_parameters() -> None:
    dsn = _postgres_dsn()
    job_id = "ltx-run-metadata-contract-recovery"
    current = InferenceJobRequest(
        job_id=job_id,
        task="video.ltx25.audio_to_video",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.mp4",
            content_type="video/mp4",
        ),
        parameters={
            "generation_profile": "test-profile",
            "prompt": "A Person speaking to the camera.",
        },
    )
    legacy_parameters = dict(current.parameters)
    legacy_parameters.update(
        run_id="901d3792b69861844519",
        run_job_count=5,
    )
    legacy = current.model_copy(update={"parameters": legacy_parameters})
    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(legacy, metadata={})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'failed',
                    attempt_count = 5,
                    last_error = %s
                WHERE job_id = %s
                """,
                (
                    "ValidationError: run_id Extra inputs are not permitted "
                    "[type=extra_forbidden]; run_job_count Extra inputs are not permitted "
                    "[type=extra_forbidden]",
                    job_id,
                ),
            )

        snapshot = queue.submit(
            current,
            metadata={"run_id": "901d3792b69861844519"},
        )

        assert snapshot.status is QueueJobStatus.PENDING
        assert snapshot.provider_payload["attempt_count"] == 0
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT request, request_sha256, run_id, run_job_count,
                       status, attempt_count, last_error
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
        assert row is not None
        request = row[0]
        assert "run_id" not in request["parameters"]
        assert "run_job_count" not in request["parameters"]
        assert row[1].strip() == current.fingerprint()
        assert row[2] == "901d3792b69861844519"
        assert row[3] == 5
        assert row[4] == "pending"
        assert row[5] == 0
        assert row[6] is None
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (job_id,))


def test_terminal_fatal_cuda_context_job_reopens_on_idempotent_resubmission() -> None:
    dsn = _postgres_dsn()
    job_id = "ltx-i2v-fatal-cuda-context-recovery"
    request = InferenceJobRequest(
        job_id=job_id,
        task="video.ltx25.generate",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.mp4",
            content_type="video/mp4",
        ),
        parameters={"prompt": "Static camera."},
    )
    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={"run_id": "fatal-cuda-test"})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'failed',
                    attempt_count = 5,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    result = NULL,
                    last_error = %s,
                    started_at = now() - interval '5 seconds',
                    finished_at = now(),
                    updated_at = now()
                WHERE job_id = %s
                """,
                (
                    "AcceleratorError: CUDA error: unknown error; "
                    "diagnostic_phase=i2v.video_decode",
                    job_id,
                ),
            )

        snapshot = queue.submit(
            request,
            metadata={"run_id": "fatal-cuda-test"},
        )

        assert snapshot.status is QueueJobStatus.PENDING
        assert snapshot.provider_payload["attempt_count"] == 0
        assert snapshot.provider_payload["last_error"] is None
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, lease_owner, lease_expires_at,
                       result, last_error, started_at, finished_at
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
        assert row == ("pending", 0, None, None, None, None, None, None)
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (job_id,))


def test_fatal_infrastructure_release_preserves_retry_budget() -> None:
    dsn = _postgres_dsn()
    job_id = "ltx-fatal-infrastructure-release-budget"
    request = InferenceJobRequest(
        job_id=job_id,
        task="video.ltx25.generate",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.mp4",
            content_type="video/mp4",
        ),
        max_attempts=1,
    )
    repository = PostgresJobRepository(dsn, max_connections=2)
    try:
        claim = repository.claim(
            request,
            request.fingerprint(),
            owner="poisoned-worker",
            lease_seconds=60,
            transport_job_id=job_id,
            instance_id=None,
        )
        assert claim.attempt_count == 1

        repository.release_infrastructure_failure(
            job_id,
            request.fingerprint(),
            owner="poisoned-worker",
            error="AcceleratorError: CUDA error: unknown error",
        )

        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, lease_owner, lease_expires_at, last_error
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "retryable_failed"
        assert row[1] == 0
        assert row[2] is None
        assert row[3] is None
        assert row[4].startswith("InfrastructureRetryable:")
        assert "CUDA error: unknown error" in row[4]

        replacement = repository.claim(
            request,
            request.fingerprint(),
            owner="replacement-worker",
            lease_seconds=60,
            transport_job_id=job_id,
            instance_id=None,
        )
        assert replacement.attempt_count == 1
    finally:
        repository.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (job_id,))


def test_partial_fatal_cuda_context_job_reopens_with_clean_budget() -> None:
    dsn = _postgres_dsn()
    job_id = "ltx-i2v-partial-fatal-cuda-context-recovery"
    request = InferenceJobRequest(
        job_id=job_id,
        task="video.ltx25.generate",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.mp4",
            content_type="video/mp4",
        ),
        parameters={"prompt": "Static camera."},
    )
    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={"run_id": "fatal-cuda-partial-test"})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'retryable_failed',
                    attempt_count = 2,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    result = NULL,
                    last_error = %s,
                    started_at = now() - interval '5 seconds',
                    finished_at = now(),
                    updated_at = now()
                WHERE job_id = %s
                """,
                (
                    "AcceleratorError: CUDA error: unknown error; "
                    "diagnostic_phase=i2v.video_decode",
                    job_id,
                ),
            )

        snapshot = queue.submit(
            request,
            metadata={"run_id": "fatal-cuda-partial-test"},
        )

        assert snapshot.status is QueueJobStatus.PENDING
        assert snapshot.provider_payload["attempt_count"] == 0
        assert snapshot.provider_payload["last_error"] is None
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, lease_owner, lease_expires_at,
                       result, last_error, started_at, finished_at
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
        assert row == ("pending", 0, None, None, None, None, None, None)
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (job_id,))


def test_identical_job_can_be_shared_across_run_ids_and_cancelled_old_run_can_reopen() -> None:
    dsn = _postgres_dsn()
    job_id = "ltx-shared-job-across-runs"
    request = InferenceJobRequest(
        job_id=job_id,
        task="video.ltx25.audio_to_video",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.mp4",
            content_type="video/mp4",
        ),
        parameters={
            "generation_profile": "test-profile",
            "prompt": "A Person speaking to the camera.",
        },
    )
    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        first = queue.submit(request, metadata={"run_id": "run-old"})
        assert first.status is QueueJobStatus.PENDING

        shared = queue.submit(request, metadata={"run_id": "run-new"})
        assert shared.status is QueueJobStatus.PENDING
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT run_id, status, attempt_count FROM gpu.jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        assert row == ("run-old", "pending", 0)

        queue.cancel(job_id)
        same_run = queue.submit(request, metadata={"run_id": "run-old"})
        assert same_run.status is QueueJobStatus.CANCELLED

        reopened = queue.submit(request, metadata={"run_id": "run-replacement"})
        assert reopened.status is QueueJobStatus.PENDING
        assert reopened.provider_payload["attempt_count"] == 0
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT run_id, status, attempt_count, lease_owner, lease_expires_at,
                       result, last_error, started_at, finished_at
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
        assert row == (
            "run-replacement", "pending", 0, None, None, None, None, None, None
        )

        conflicting = request.model_copy(
            update={"parameters": {**request.parameters, "prompt": "Different prompt."}}
        )
        with pytest.raises(RuntimeError, match="different request"):
            queue.submit(conflicting, metadata={"run_id": "run-conflict"})
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (job_id,))
