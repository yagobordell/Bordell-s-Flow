from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from scripts.pipeline import preflight_video_factory as preflight

def _services() -> dict:
    return {
        "stack": {
            "organization": "org",
            "project": "project",
        },
        "services": {
            "whisper": {"queue_name": "whisper-q"},
            "ltx25": {"queue_name": "ltx-q"},
        },
    }


def test_salad_queue_preflight_allows_only_manifest_owned_ltx_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "phase8" / "video_generation_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "transport_job_id": "transport-owned",
                        "transport_status": "running",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")

    def fake_summary(*, base_url: str, queue_name: str, api_key: str):
        assert base_url.endswith("/organizations/org/projects/project")
        assert api_key == "test-key"
        return {"current_queue_length": 1 if queue_name == "ltx-q" else 0}

    def fake_jobs(*, base_url: str, queue_name: str, api_key: str):
        assert base_url.endswith("/organizations/org/projects/project")
        assert api_key == "test-key"
        if queue_name == "ltx-q":
            return [{"id": "transport-owned", "status": "running"}]
        return []

    monkeypatch.setattr(preflight, "_queue_summary", fake_summary)
    monkeypatch.setattr(preflight, "_queue_jobs", fake_jobs)

    result = preflight._check_salad_queues(_services(), tmp_path)

    assert result["ltx25"] == {
        "active_jobs": 1,
        "recognized_resume_jobs": 1,
    }
    assert result["whisper"]["active_jobs"] == 0



def test_salad_queue_preflight_allows_manifest_owned_realesrgan_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "phase8" / "video_upscale_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "transport_job_id": "upscale-owned",
                        "transport_status": "pending",
                    },
                    {
                        "transport_job_id": "upscale-succeeded",
                        "transport_status": "succeeded",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")
    services = {
        "stack": {"organization": "org", "project": "project"},
        "services": {"realesrgan": {"queue_name": "realesrgan-q"}},
    }
    monkeypatch.setattr(
        preflight,
        "_queue_summary",
        lambda **_: {"current_queue_length": 1},
    )
    monkeypatch.setattr(
        preflight,
        "_queue_jobs",
        lambda **_: [
            {"id": "upscale-owned", "status": "pending"},
        ],
    )

    result = preflight._check_salad_queues(services, tmp_path)

    assert result["realesrgan"] == {
        "active_jobs": 1,
        "recognized_resume_jobs": 1,
    }


def test_salad_queue_preflight_rejects_unowned_active_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")
    monkeypatch.setattr(
        preflight,
        "_queue_summary",
        lambda **_: {"current_queue_length": 1},
    )
    monkeypatch.setattr(
        preflight,
        "_queue_jobs",
        lambda **_: [{"id": "foreign-job", "status": "pending"}],
    )

    with pytest.raises(RuntimeError, match="not owned by the local resume manifest"):
        preflight._check_salad_queues(_services(), tmp_path)


def test_resume_transport_reader_rejects_malformed_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "phase8" / "video_generation_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"jobs": "not-a-list"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid jobs"):
        preflight._active_resume_transport_ids(tmp_path)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_salad_queue_preflight_retries_transient_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("The read operation timed out")
        return _FakeResponse({"items": []})

    monkeypatch.setattr(preflight.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(preflight.time, "sleep", sleeps.append)

    jobs = preflight._queue_jobs(
        base_url="https://api.salad.com/api/public/organizations/org/projects/project",
        queue_name="ltx-q",
        api_key="test-key",
    )

    assert jobs == []
    assert attempts == 2
    assert sleeps == [2]


def test_salad_queue_preflight_retries_503_then_fails_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Server Unavailable",
            hdrs=None,
            fp=io.BytesIO(b"unavailable"),
        )

    monkeypatch.setattr(preflight.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(preflight.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="Salad queue preflight failed for ltx-q: HTTP 503"):
        preflight._queue_jobs(
            base_url="https://api.salad.com/api/public/organizations/org/projects/project",
            queue_name="ltx-q",
            api_key="test-key",
        )

    assert attempts == 6
    assert sleeps == [2, 4, 6, 8, 10]


def test_salad_queue_preflight_skips_job_history_for_empty_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")
    monkeypatch.setattr(
        preflight,
        "_queue_summary",
        lambda **_: {"current_queue_length": 0},
    )

    def fail_jobs(**_):
        raise AssertionError("job history must not be read for an empty queue summary")

    monkeypatch.setattr(preflight, "_queue_jobs", fail_jobs)

    result = preflight._check_salad_queues(_services(), tmp_path)

    assert result["whisper"]["active_jobs"] == 0
    assert result["ltx25"]["active_jobs"] == 0


def test_salad_queue_preflight_enumerates_stale_positive_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")
    monkeypatch.setattr(
        preflight,
        "_queue_summary",
        lambda **_: {"current_queue_length": 1},
    )
    monkeypatch.setattr(preflight, "_queue_jobs", lambda **_: [])

    result = preflight._check_salad_queues(_services(), tmp_path)

    assert result["whisper"]["active_jobs"] == 0
    assert result["ltx25"]["active_jobs"] == 0


def test_salad_queue_preflight_falls_back_to_jobs_when_summary_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")

    def fail_summary(**_):
        raise preflight.TransientSaladPreflightError(
            "Salad queue summary preflight failed: timed out"
        )

    monkeypatch.setattr(preflight, "_queue_summary", fail_summary)
    monkeypatch.setattr(preflight, "_queue_jobs", lambda **_: [])

    result = preflight._check_salad_queues(_services(), tmp_path)

    assert result["whisper"]["active_jobs"] == 0
    assert result["ltx25"]["active_jobs"] == 0


def test_salad_queue_preflight_defers_when_both_transient_routes_are_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")

    def fail_summary(**_):
        raise preflight.TransientSaladPreflightError("summary timed out")

    def fail_jobs(**_):
        raise preflight.TransientSaladPreflightError("jobs timed out")

    monkeypatch.setattr(preflight, "_queue_summary", fail_summary)
    monkeypatch.setattr(preflight, "_queue_jobs", fail_jobs)

    result = preflight._check_salad_queues(_services(), tmp_path)

    assert result["whisper"]["verification"] == "deferred_transient"
    assert result["ltx25"]["verification"] == "deferred_transient"
    assert result["ltx25"]["active_jobs"] is None


def test_hard_salad_queue_guard_refuses_transient_defer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")

    def fail_summary(**_):
        raise preflight.TransientSaladPreflightError("summary timed out")

    def fail_jobs(**_):
        raise preflight.TransientSaladPreflightError("jobs timed out")

    monkeypatch.setattr(preflight, "_queue_summary", fail_summary)
    monkeypatch.setattr(preflight, "_queue_jobs", fail_jobs)

    with pytest.raises(preflight.TransientSaladPreflightError, match="jobs timed out"):
        preflight._check_salad_queues(
            _services(),
            tmp_path,
            service_names={"ltx25"},
            allow_transient_defer=False,
        )


def test_salad_queue_summary_fails_over_after_two_short_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        assert timeout == 10.0
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(preflight.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(preflight.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="Salad queue summary preflight failed for ltx-q"):
        preflight._queue_summary(
            base_url="https://api.salad.com/api/public/organizations/org/projects/project",
            queue_name="ltx-q",
            api_key="test-key",
        )

    assert attempts == 2
    assert sleeps == [2]


def test_transient_salad_retry_exhaustion_uses_distinct_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request, timeout):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(preflight.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(preflight.time, "sleep", lambda _: None)

    with pytest.raises(preflight.TransientSaladPreflightError):
        preflight._queue_summary(
            base_url="https://api.salad.com/api/public/organizations/org/projects/project",
            queue_name="ltx-q",
            api_key="test-key",
        )
