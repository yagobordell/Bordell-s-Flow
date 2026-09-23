import time

from fastapi.testclient import TestClient

from ai_video_factory.inference.app import create_app
from ai_video_factory.inference.errors import ModelBootstrapPendingError


class _PrepareWorker:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.prepare_calls = 0
        self.ready_calls = 0
        self.closed = False

    def prepare(self) -> None:
        self.prepare_calls += 1
        if self.failures:
            raise self.failures.pop(0)

    def ready(self) -> None:
        self.ready_calls += 1

    def close(self) -> None:
        self.closed = True


def test_background_prepare_retries_explicit_model_bootstrap_wait() -> None:
    worker = _PrepareWorker([ModelBootstrapPendingError("model bootstrap pending")])

    with TestClient(
        create_app(
            worker,  # type: ignore[arg-type]
            prepare_in_background=True,
            prepare_retry_seconds=0.01,
        )
    ) as client:
        deadline = time.monotonic() + 1
        response = client.get("/ready")
        while response.status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.01)
            response = client.get("/ready")

        assert response.json() == {"status": "ready"}
        assert worker.prepare_calls >= 2
        assert worker.ready_calls >= 1

    assert worker.closed is True


def test_background_prepare_does_not_retry_arbitrary_file_not_found() -> None:
    worker = _PrepareWorker([FileNotFoundError("hugging face cache miss")])

    with TestClient(
        create_app(
            worker,  # type: ignore[arg-type]
            prepare_in_background=True,
            prepare_retry_seconds=0.01,
        )
    ) as client:
        deadline = time.monotonic() + 1
        response = client.get("/ready")
        while worker.prepare_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
            response = client.get("/ready")

        assert response.status_code == 503
        time.sleep(0.05)
        assert worker.prepare_calls == 1

    assert worker.closed is True
