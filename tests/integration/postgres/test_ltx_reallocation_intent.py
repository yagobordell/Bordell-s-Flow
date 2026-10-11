"""Postgres tests for claim fencing and durable one-shot Salad move intents."""

from __future__ import annotations

import os

import psycopg
import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.errors import JobBusyError
from ai_video_factory.inference.repository import PostgresJobRepository
from ai_video_factory.inference.salad_autoscaler import PostgresAutoscalerStore


def _dsn() -> str:
    value = os.getenv("TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    return value


def test_reallocation_intent_fences_claim_and_survives_normal_autoscaler_cleanup():
    dsn = _dsn()
    instance_id = "test-remediation-fenced-001"
    stage = "ltx25_i2v"
    request = InferenceJobRequest(
        job_id="test-remediation-claim-001", task="video.ltx25.generate",
        output=ObjectOutput(
            key="jobs/test-remediation-claim-001/out.mp4",
            content_type="video/mp4",
        ),
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM gpu.capacity_drains WHERE instance_id = %s", (instance_id,),
        )
        conn.execute(
            "DELETE FROM gpu.ltx_instance_reallocations WHERE instance_id = %s",
            (instance_id,),
        )
        conn.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (request.job_id,))

    store = PostgresAutoscalerStore(dsn)
    repo = PostgresJobRepository(dsn)
    try:
        assert store.begin_ltx_reallocation_drain(
            stage=stage, instance_id=instance_id, ttl_seconds=10,
            original_machine_id="original-salad-node",
        )
        assert not store.begin_ltx_reallocation_drain(
            stage=stage, instance_id=instance_id, ttl_seconds=10,
        )
        assert store.list_ltx_reallocation_intents(stage=stage)[
            instance_id
        ]["original_machine_id"] == "original-salad-node"
        with pytest.raises(JobBusyError, match="draining"):
            repo.claim(
                request, request.fingerprint(),
                owner=f"worker-{instance_id}", lease_seconds=60,
                transport_job_id=None, instance_id=instance_id,
            )
        # Ordinary downscale reset must not break the claim fence.
        store.clear_draining_instances(stage=stage)
        assert instance_id in store.list_draining_instances(stage=stage)
        assert store.request_ltx_reallocation(stage=stage, instance_id=instance_id)
        assert not store.request_ltx_reallocation(
            stage=stage, instance_id=instance_id
        )
        store.clear_draining_instances(stage=stage)
        assert instance_id in store.list_draining_instances(stage=stage)
        store.complete_ltx_reallocation(stage=stage, instance_id=instance_id)
        assert instance_id not in store.list_draining_instances(stage=stage)
        assert store.list_ltx_reallocation_intents(stage=stage)[
            instance_id
        ]["state"] == "recovered"
    finally:
        repo.close()
        store.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM gpu.capacity_drains WHERE instance_id = %s",
                (instance_id,),
            )
            conn.execute(
                "DELETE FROM gpu.ltx_instance_reallocations WHERE instance_id = %s",
                (instance_id,),
            )
            conn.execute("DELETE FROM gpu.jobs WHERE job_id = %s", (request.job_id,))
