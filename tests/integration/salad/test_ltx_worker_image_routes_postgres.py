"""Real PostgreSQL proof that concurrent Docker versions cannot steal jobs."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.repository import PostgresJobRepository
from ai_video_factory.inference.salad_autoscaler import (
    AutoscalerServiceBinding,
    PostgresAutoscalerStore,
)
from ai_video_factory.inference.worker_image_routes import (
    register_route,
    route_for_image,
)
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_DSN"), reason="Requires CI PostgreSQL migrations"
)
PREFIX = "docker.io/yagobordell/ai-video-factory@sha256:"


def test_two_user_routes_share_one_db_but_never_one_task():
    import psycopg

    dsn = os.environ["TEST_POSTGRES_DSN"]
    a, b = PREFIX + uuid.uuid4().hex * 2, PREFIX + uuid.uuid4().hex * 2
    expected = route_for_image("ltx25", a)
    with ThreadPoolExecutor(max_workers=4) as pool:
        registrations = list(
            pool.map(
                lambda _: register_route(dsn, service="ltx25", image=a),
                range(4),
            )
        )
    assert registrations == [expected] * 4
    assert register_route(dsn, service="ltx25", image=b) != expected

    unique = uuid.uuid4().hex[:10]
    requests = [
        InferenceJobRequest(
            job_id=f"versiontest-{unique}-{label}",
            task=route_for_image("ltx25", image).task_name,
            output=ObjectOutput(
                key=f"jobs/versiontest-{unique}-{label}/clip.mp4",
                content_type="video/mp4",
            ),
            parameters={"required_worker_image": image},
        )
        for label, image in (("a", a), ("b", b))
    ]
    queue = PostgresJobQueueClient(dsn=dsn)
    workers = PostgresJobRepository(dsn)
    store = PostgresAutoscalerStore(dsn)
    try:
        for request in requests:
            queue.submit(request, metadata={})
        for selected_image, request in zip((a, b), requests, strict=True):
            expected_task = route_for_image("ltx25", selected_image).task_name
            pending = workers.next_pending_request(
                (expected_task,), required_worker_image=selected_image,
            )
            assert pending is not None and pending.job_id == request.job_id
            binding = AutoscalerServiceBinding(
                workload=expected_task,
                task_names=(expected_task,),
                autoscaler_env_prefix="LTX25",
                fallback_runtime_seconds=206.0,
                max_replicas=4,
                required_worker_image=selected_image,
            )
            rows = store.list_stage_jobs(
                binding=binding, statuses=("pending", "running", "retryable_failed"),
            )
            assert [row["job_id"] for row in rows] == [request.job_id]
        # An old worker polling the unversioned task cannot see either request.
        base = workers.next_pending_request(("video.ltx25.audio_to_video",))
        assert base is None or base.job_id not in {req.job_id for req in requests}
    finally:
        store.close()
        workers.close()
        queue.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM gpu.jobs WHERE job_id = ANY(%s)",
                ([req.job_id for req in requests],),
            )
            connection.execute(
                "DELETE FROM gpu.worker_image_routes WHERE service = %s "
                "AND image = ANY(%s)",
                ("ltx25", [a, b]),
            )
