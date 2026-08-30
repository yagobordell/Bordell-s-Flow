from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "replay_phase7_smoke.py"
    spec = importlib.util.spec_from_file_location("replay_phase7_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay = _load_script()


def _request() -> dict:
    return {
        "input": {
            "job_id": "phase7-smoke-123",
            "task": "infrastructure.copy",
            "inputs": [{"name": "source", "key": "input.txt", "sha256": "a" * 64}],
            "output": {"key": "output.txt", "content_type": "text/plain"},
        },
        "metadata": {"application_job_id": "phase7-smoke-123", "phase": "7"},
    }


def _succeeded_job(*, replayed: bool = True, attempt_count: int = 1) -> dict:
    return {
        "status": "succeeded",
        "output": {
            "job_id": "phase7-smoke-123",
            "status": "succeeded",
            "replayed": replayed,
            "attempt_count": attempt_count,
            "output": {
                "key": "output.txt",
                "content_type": "text/plain",
                "sha256": "a" * 64,
            },
        },
    }


def test_validate_replay_accepts_same_artifact_without_new_attempt() -> None:
    output = replay._validate_replay(
        _succeeded_job(),
        _request(),
        expected_attempt_count=1,
    )

    assert output["replayed"] is True
    assert output["attempt_count"] == 1


def test_validate_replay_rejects_normal_execution() -> None:
    with pytest.raises(RuntimeError, match="replayed=true"):
        replay._validate_replay(
            _succeeded_job(replayed=False),
            _request(),
            expected_attempt_count=1,
        )


def test_validate_replay_rejects_extra_attempt() -> None:
    with pytest.raises(RuntimeError, match="changed attempt_count"):
        replay._validate_replay(
            _succeeded_job(attempt_count=2),
            _request(),
            expected_attempt_count=1,
        )
