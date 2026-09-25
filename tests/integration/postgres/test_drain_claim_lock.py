from __future__ import annotations

import os
import threading
import time

import psycopg
import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.errors import JobBusyError
from ai_video_factory.inference.repository import PostgresJobRepository
from ai_video_factory.inference.salad_autoscaler import publish_instance_drains


def _postgres_dsn() -> str:
    dsn = os.getenv("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    return dsn


def test_real_postgres_drain_commit_fences_concurrent_worker_claim() -> None:
    dsn = _postgres_dsn()
    instance_id = "instance-real-postgres-race"
    request = InferenceJobRequest(
        job_id="postgres-drain-race-job",
        task="test.postgres_race",
        output=ObjectOutput(
            key="jobs/postgres-drain-race-job/output.bin",
            content_type="application/octet-stream",
        ),
    )

    with psycopg.connect(dsn, autocommit=True) as cleanup:
        cleanup.execute("DELETE FROM gpu.capacity_drains")
        cleanup.execute("DELETE FROM gpu.jobs")

    repository = PostgresJobRepository(dsn, max_connections=2)
    outcome: dict[str, object] = {}
    started = threading.Event()

    def claim() -> None:
        started.set()
        try:
            outcome["claim"] = repository.claim(
                request,
                request.fingerprint(),
                owner=f"worker-{instance_id}",
                lease_seconds=60,
                transport_job_id=None,
                instance_id=instance_id,
            )
        except Exception as error:
            outcome["error"] = error

    thread = threading.Thread(target=claim, daemon=True)

    try:
        with psycopg.connect(dsn) as connection:
            with connection.transaction():
                publish_instance_drains(
                    connection,
                    stage="ltx25",
                    instance_ids=(instance_id,),
                    ttl_seconds=120.0,
                )
                thread.start()
                assert started.wait(timeout=1)
                time.sleep(0.2)
                assert thread.is_alive(), (
                    "claim should wait for the transaction-scoped instance advisory lock"
                )

        thread.join(timeout=3)
        assert not thread.is_alive()
        assert isinstance(outcome.get("error"), JobBusyError)
        assert "claim" not in outcome

        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT status FROM gpu.jobs WHERE job_id = %s",
                (request.job_id,),
            ).fetchone()
        assert row is None
    finally:
        repository.close()
