"""Terminal experimental module corruption must quarantine the running worker."""
from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.errors import NonRetryableTaskError


class _StructuralWorker:
    def __init__(self) -> None:
        self.claims = 0
        self.closed = False

    def prepare(self) -> None:
        pass

    def ready(self) -> None:
        pass

    def next_pending_request(self) -> SimpleNamespace:
        self.claims += 1
        return SimpleNamespace(
            task="video.ltx25.audio_to_video_experimental",
            job_id="experimental-structural-failure",
        )

    def process(self, *_args: object, **_kwargs: object) -> None:
        raise NonRetryableTaskError(
            "A2V_EXPERIMENTAL_STREAMING_STRUCTURE_INVALID: live model unsafe"
        )

    def close(self) -> None:
        self.closed = True


def test_worker_halts_queue_and_loses_readiness_after_structural_failure() -> None:
    worker = _StructuralWorker()
    with TestClient(
        create_app(
            worker,  # type: ignore[arg-type]
            poll_jobs_from_repository=True,
            job_poll_seconds=0.01,
        )
    ) as client:
        deadline = time.monotonic() + 2
        while worker.claims == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.claims == 1
        assert client.get("/ready").status_code == 503
        time.sleep(0.05)
        assert worker.claims == 1  # No additional work on corrupted process.
    assert worker.closed is True
