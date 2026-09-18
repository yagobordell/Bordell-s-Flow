from __future__ import annotations

import io
import urllib.error
import urllib.request
from typing import Any

import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.providers.job_queue import QueueJobNotFoundError, TransientQueueError
from ai_video_factory.providers.salad_queue import SaladJobQueueClient


def _client() -> SaladJobQueueClient:
    return SaladJobQueueClient(
        organization="org",
        project="project",
        queue_name="queue",
        api_key="secret",
        timeout_seconds=0.01,
    )


def _request() -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id="job-001",
        task="image.test",
        output=ObjectOutput(
            key="jobs/job-001/image.png",
            content_type="image/png",
        ),
    )


def test_queue_get_classifies_404_as_missing_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            url="https://example.invalid/jobs/transport-404",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b'{"title":"Not Found"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(QueueJobNotFoundError, match="HTTP 404"):
        _client().get("transport-404")


def test_queue_get_classifies_read_timeout_as_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fail(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(TransientQueueError, match="GET request timed out"):
        _client().get("transport-001")

    assert calls == 1


def test_queue_submit_does_not_treat_timeout_as_safe_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(RuntimeError, match="POST request timed out"):
        _client().submit(_request(), metadata={"phase": "4"})

    assert calls == 1


def test_queue_cancel_uses_delete_and_accepts_empty_202_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class EmptyResponse:
        def __enter__(self) -> EmptyResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def succeed(request: urllib.request.Request, *, timeout: float) -> EmptyResponse:
        captured["method"] = request.get_method()
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return EmptyResponse()

    monkeypatch.setattr(urllib.request, "urlopen", succeed)

    _client().cancel("transport-001")

    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/queues/queue/jobs/transport-001")
    assert captured["timeout"] == 0.01


def test_queue_cancel_timeout_is_not_retried_implicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(RuntimeError, match="DELETE request timed out"):
        _client().cancel("transport-001")

    assert calls == 1
