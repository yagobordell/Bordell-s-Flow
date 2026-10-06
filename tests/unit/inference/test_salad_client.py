from __future__ import annotations

import pytest

import ai_video_factory.inference.salad as salad
from ai_video_factory.inference.salad import (
    READ_MAX_ATTEMPTS,
    SaladClient,
    SaladDispatchConfig,
    SaladTransientRequestError,
)


class _Response:
    def __init__(self, body: str, *, status: int = 200) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _client(
    *,
    warnings: list[str] | None = None,
    retry_observer=None,
) -> SaladClient:
    return SaladClient(
        SaladDispatchConfig(
            api_key="test-key",
            api_base_url="https://api.salad.test/api/public",
            organization_name="test-org",
            project_name="test-project",
            container_group_name="test-group",
        ),
        warning_logger=(warnings.append if warnings is not None else lambda _message: None),
        retry_observer=retry_observer,
    )


def test_get_retries_timeout_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    delays: list[float] = []
    warnings: list[str] = []
    retry_observations: list[str] = []

    def fake_urlopen(*_args: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("The read operation timed out")
        return _Response('{"items":[{"name":"test-group"}]}')

    monkeypatch.setattr(salad, "urlopen", fake_urlopen)
    monkeypatch.setattr(salad.time, "sleep", delays.append)

    groups = _client(
        warnings=warnings,
        retry_observer=lambda: retry_observations.append("heartbeat"),
    ).list_project_container_groups()

    assert groups == [{"name": "test-group"}]
    assert calls == 3
    assert delays == [2, 4]
    assert len(warnings) == 2
    assert retry_observations == ["heartbeat", "heartbeat"]
    assert all("Salad GET transient failure" in warning for warning in warnings)


def test_get_raises_after_transient_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    delays: list[float] = []

    def fake_urlopen(*_args: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(salad, "urlopen", fake_urlopen)
    monkeypatch.setattr(salad.time, "sleep", delays.append)

    with pytest.raises(SaladTransientRequestError, match="read operation timed out"):
        _client().list_project_container_groups()

    assert calls == READ_MAX_ATTEMPTS
    assert delays == [2, 4, 6, 8, 10]


def test_mutating_request_does_not_retry_transport_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_urlopen(*_args: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(salad, "urlopen", fake_urlopen)

    with pytest.raises(SaladTransientRequestError, match="read operation timed out"):
        _client().stop_container_group()

    assert calls == 1
