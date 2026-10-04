"""No-GPU guard for the exact Docker image before a billed Salad I2V startup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.smoke import verify_ltx25_i2v_hybrid_image as verify

PINNED = "docker.io/yagobordell/ai-video-factory@sha256:" + "b" * 64
ROOT = Path(__file__).resolve().parents[3]


def test_image_probe_reads_installed_code_inside_exact_immutable_image() -> None:
    with patch.object(
        verify.subprocess,
        "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="I2V_HYBRID_DOCKER_ABI_OK\n",
            stderr="",
        ),
    ) as run:
        verify.verify(PINNED)
    argv = run.call_args.args[0]
    assert argv[:6] == [
        "docker", "run", "--rm", "--network", "none", "--entrypoint",
    ]
    assert argv[6:9] == ["python", PINNED, "-c"]
    code = argv[9]
    assert "Batch2Parameters.model_fields" in code
    assert 'full_hd_stage2_mode' in code
    assert "_full_hd_streaming_stage2" in code
    assert "stage2_factory" in code
    assert "I2V_HYBRID_DOCKER_ABI_OK" in code
    assert run.call_args.kwargs["timeout"] >= 300
    assert "gpus" not in argv


def test_old_disk_image_fails_closed_before_paid_start() -> None:
    with patch.object(
        verify.subprocess,
        "run",
        return_value=SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="AssertionError: old batch2 request schema",
        ),
    ):
        with pytest.raises(RuntimeError, match="Rebuild the image"):
            verify.verify(PINNED)


def test_missing_docker_or_image_fails_closed() -> None:
    with patch.object(verify.subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="NO Salad GPU must be started"):
            verify.verify(PINNED)
    with patch.object(
        verify.subprocess, "run",
        side_effect=subprocess.TimeoutExpired("docker", 900),
    ):
        with pytest.raises(RuntimeError, match="NO Salad GPU must be started"):
            verify.verify(PINNED)


@pytest.mark.parametrize("image", ["tag:latest", "repo@sha256:deadbeef", ""])
def test_must_pass_exact_digest(image: str) -> None:
    with patch.object(verify.subprocess, "run") as run:
        with pytest.raises(ValueError, match="immutable"):
            verify.verify(image)
        run.assert_not_called()


def test_powershell_checks_docker_abi_before_it_starts_salad() -> None:
    source = (
        ROOT / "scripts/smoke/run_ltx25_i2v_batch2_trial.ps1"
    ).read_text(encoding="utf-8")
    guard = "verify_ltx25_i2v_hybrid_image.py"
    start = "$Started = $false"
    assert source.index(guard) < source.index(start)
    assert "STOP: pinned Docker lacks the hybrid I2V API" in source
    # Local --CheckInputsOnly must still remain free and require no Docker pull.
    assert source.index("if ($CheckInputsOnly)") < source.index(guard)
