from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.providers.job_queue import QueueJobStatus
from ai_video_factory.providers.salad_queue import SaladJobQueueClient


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _request() -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id="ideogram-reference-submit-recovery",
        task="image.reference",
        output=ObjectOutput(
            key="jobs/ideogram-reference-submit-recovery/image.png",
            content_type="image/png",
        ),
        parameters={"seed": 123},
    )


def _client() -> SaladJobQueueClient:
    return SaladJobQueueClient(
        organization="org",
        project="project",
        queue_name="queue",
        api_key="test-key",
        timeout_seconds=1.0,
    )


def _queue_job(
    request: InferenceJobRequest,
    *,
    transport_id: str = "transport-1",
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "id": transport_id,
        "status": status,
        "input": request.model_dump(mode="json", exclude_none=True),
        "metadata": {
            "application_job_id": request.job_id,
            "request_sha256": request.fingerprint(),
        },
    }


def test_submit_adds_deterministic_recovery_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    captured: dict[str, Any] = {}

    def fake_urlopen(http_request, timeout):
        captured["method"] = http_request.get_method()
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return _FakeResponse(_queue_job(request))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    snapshot = _client().submit(request, metadata={"phase": "4"})

    assert snapshot.id == "transport-1"
    assert snapshot.status == QueueJobStatus.PENDING
    assert captured["method"] == "POST"
    assert captured["body"]["metadata"] == {
        "phase": "4",
        "application_job_id": request.job_id,
        "request_sha256": request.fingerprint(),
    }


def test_submit_timeout_recovers_existing_transport_without_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    methods: list[str] = []

    def fake_urlopen(http_request, timeout):
        method = http_request.get_method()
        methods.append(method)
        if method == "POST":
            raise TimeoutError("The read operation timed out")
        return _FakeResponse({"items": [_queue_job(request)]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda _: None)

    snapshot = _client().submit(request, metadata={"phase": "4"})

    assert snapshot.id == "transport-1"
    assert snapshot.status == QueueJobStatus.PENDING
    assert methods.count("POST") == 1
    assert methods.count("GET") == 1


def test_submit_503_recovers_existing_transport_without_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    methods: list[str] = []

    def fake_urlopen(http_request, timeout):
        method = http_request.get_method()
        methods.append(method)
        if method == "POST":
            raise urllib.error.HTTPError(
                http_request.full_url,
                503,
                "Server Unavailable",
                hdrs=None,
                fp=io.BytesIO(b"unavailable"),
            )
        return _FakeResponse({"items": [_queue_job(request, status="running")]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda _: None)

    snapshot = _client().submit(request, metadata={"phase": "4"})

    assert snapshot.status == QueueJobStatus.RUNNING
    assert methods.count("POST") == 1
    assert methods.count("GET") == 1


def test_submit_never_reposts_when_timeout_cannot_be_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    methods: list[str] = []

    def fake_urlopen(http_request, timeout):
        method = http_request.get_method()
        methods.append(method)
        if method == "POST":
            raise TimeoutError("The read operation timed out")
        return _FakeResponse({"items": []})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="refusing unsafe duplicate POST"):
        _client().submit(request, metadata={"phase": "4"})

    assert methods.count("POST") == 1
    assert methods.count("GET") == 6


def test_recovery_ignores_failed_historical_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    methods: list[str] = []

    def fake_urlopen(http_request, timeout):
        method = http_request.get_method()
        methods.append(method)
        if method == "POST":
            raise TimeoutError("The read operation timed out")
        return _FakeResponse(
            {
                "items": [
                    _queue_job(
                        request,
                        transport_id="old-failed",
                        status="failed",
                    )
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="refusing unsafe duplicate POST"):
        _client().submit(request, metadata={"phase": "4"})

    assert methods.count("POST") == 1
