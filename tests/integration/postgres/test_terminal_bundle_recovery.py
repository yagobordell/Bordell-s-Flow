from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from ai_video_factory.inference.bundle_publication import (
    bundle_manifest_key,
    stage_bundle,
)
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient


def _postgres_dsn() -> str:
    dsn = os.getenv("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    return dsn


@pytest.mark.parametrize("terminal_status", ["retryable_failed", "failed"])
def test_committed_bundle_recovers_terminal_postgres_without_new_attempt(
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
