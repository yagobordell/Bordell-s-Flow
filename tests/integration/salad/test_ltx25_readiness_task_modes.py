"""Regression coverage for strict LTX readiness when borrowing a production group."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_READY_PATH = _ROOT / "scripts" / "smoke" / "wait_salad_ltx25_ready.py"
_DIGEST = "docker.io/yagobordell/ai-video-factory@sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("service", "reuse", "pinned", "exit_code", "expected_mode"),
    [
        ("ltx25", False, False, 0, "mode=a2v"),
        ("ltx25_a2v_experimental", False, False, 0, "mode=a2v_experimental"),
        ("ltx25", True, True, 0, "mode=a2v_experimental"),
        ("ltx25", True, False, 1, ""),
        ("ltx25_i2v", True, True, 1, ""),
        ("ltx25_a2v_experimental", True, True, 1, ""),
    ],
)
def test_readiness_configuration_requires_explicit_valid_reuse(
    tmp_path: Path,
    service: str,
    reuse: bool,
    pinned: bool,
    exit_code: int,
    expected_mode: str,
) -> None:
    env_file = tmp_path / "salad.env"
    env_file.write_text("# intentionally no real credentials\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        SALAD_ORGANIZATION="test-organization",
        SALAD_PROJECT="test-project",
        SALAD_API_KEY="test-only-placeholder",
    )
    args = [
        sys.executable,
        str(_READY_PATH),
        "--env-file",
        str(env_file),
        "--service",
        service,
        "--check-config-only",
    ]
    if pinned:
        args.extend(["--expected-image", _DIGEST])
    if reuse:
        args.append("--reuse-production")
    result = subprocess.run(
        args, cwd=_ROOT, env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    if exit_code == 0:
        assert expected_mode in result.stdout


def test_observation_rejects_production_task_mode_on_reused_experimental_image() -> None:
    spec = importlib.util.spec_from_file_location("_a2v_readiness", _READY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    group = {
        "name": "ltx25",
        "readiness_probe": {"http": {"path": "/ready"}},
        "container": {
            "image": _DIGEST,
            "environment_variables": {"LTX_WORKER_TASK_MODE": "a2v_experimental"},
        },
        "version": 2,
        "current_state": {"status": "running"},
        "pending_change": False,
        "replicas": 1,
    }
    instances = [
        {"id": "worker", "version": 2, "state": "running", "started": True, "ready": True}
    ]
    expected_id, _ = module._readiness_observation(
        group,
        instances,
        group_name="ltx25",
        image_repository=_DIGEST.split("@")[0],
        expected_image=_DIGEST,
        expected_task_mode="a2v_experimental",
    )
    assert expected_id == "worker"
    with pytest.raises(RuntimeError, match="wrong LTX_WORKER_TASK_MODE"):
        module._readiness_observation(
            group,
            instances,
            group_name="ltx25",
            image_repository=_DIGEST.split("@")[0],
            expected_image=_DIGEST,
            expected_task_mode="a2v",
        )
