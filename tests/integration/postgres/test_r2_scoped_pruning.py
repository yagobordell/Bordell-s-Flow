from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg
import pytest

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    ObjectInput,
    ObjectOutput,
)
from ai_video_factory.providers.job_queue import QueueJobStatus
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient


def _dsn() -> str:
    value = os.getenv("TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("TEST_POSTGRES_DSN is required")
    return value


def test_r2_scoped_pruning_preserves_history_and_allows_replay() -> None:
    dsn = _dsn()
    scope = "f" * 24
    job_id = f"r2-cleanup-test-r{scope}"
    request = InferenceJobRequest(
        job_id=job_id,
        task="video.ltx25.generate",
        inputs=[ObjectInput(
            name="keyframe", key=f"jobs/{job_id}/inputs/keyframe.png",
        )],
        output=ObjectOutput(
            key=f"jobs/{job_id}/clip.mp4", content_type="video/mp4",
        ),
    )
    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={"run_id": "test-r2-cleanup"})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status='succeeded', started_at=now()-interval '15 seconds',
                    finished_at=now(), attempt_count=1
                WHERE job_id=%s
                """,
                (job_id,),
            )

        with queue.prune_scoped_video_run(scope, [job_id]) as ids:
            assert ids == [job_id]

        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT status, r2_pruned_at, started_at, finished_at
                FROM gpu.jobs WHERE job_id=%s
                """,
                (job_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "succeeded"
        assert row[1] is not None
        assert row[2] is not None
        assert row[3] is not None
        assert row[1] <= datetime.now(UTC)

        snapshot = queue.submit(request, metadata={"run_id": "test-r2-cleanup"})
        assert snapshot.status is QueueJobStatus.PENDING
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT r2_pruned_at, status, attempt_count FROM gpu.jobs WHERE job_id=%s",
                (job_id,),
            ).fetchone()
        assert row == (None, "pending", 0)
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM gpu.jobs WHERE job_id=%s", (job_id,))


def test_r2_pruning_rejects_shared_inputs_without_touching_rows() -> None:
    dsn = _dsn()
    scope = "e" * 24
    job_id = f"r2-shared-test-r{scope}"
    request = InferenceJobRequest(
        job_id=job_id,
        task="video.ltx25.generate",
        inputs=[ObjectInput(
            name="keyframe", key="b2-ltx/inputs/images/shared.png",
        )],
        output=ObjectOutput(
            key=f"jobs/{job_id}/clip.mp4", content_type="video/mp4",
        ),
    )
    queue = PostgresJobQueueClient(dsn=dsn, max_connections=2)
    try:
        queue.submit(request, metadata={})
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                "UPDATE gpu.jobs SET status='succeeded' WHERE job_id=%s",
                (job_id,),
            )
        with pytest.raises(RuntimeError, match="unscoped"):
            with queue.prune_scoped_video_run(scope, [job_id]):
                pytest.fail("Unsafe shared R2 keys must not reach deletion")
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT status, r2_pruned_at FROM gpu.jobs WHERE job_id=%s",
                (job_id,),
            ).fetchone()
        assert row == ("succeeded", None)
    finally:
        queue.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM gpu.jobs WHERE job_id=%s", (job_id,))
