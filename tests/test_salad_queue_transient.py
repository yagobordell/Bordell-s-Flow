from __future__ import annotations

import urllib.request

import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.providers.job_queue import TransientQueueError
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
