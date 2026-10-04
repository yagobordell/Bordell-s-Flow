"""No-GPU guard for the exact Docker image before a billed Salad I2V startup."""

from __future__ import annotations

import ast
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
    assert "STAGE2_SIGMAS_SAFE_ABI" in code
    assert "stage2_sigmas" in code
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
    start = "$StartRequested = $false"
    assert source.index(guard) < source.index(start)
    assert "STOP: pinned Docker lacks the hybrid I2V API" in source
    # Local --CheckInputsOnly must still remain free and require no Docker pull.
    assert source.index("if ($CheckInputsOnly)") < source.index(guard)


def test_stage2_sigma_tensor_is_never_shadowed_by_lazy_stage_object() -> None:
    """CPU-only AST gate: even CI without torch must catch the paid-trial bug."""
    path = ROOT / "src/ai_video_factory/workers/ltx25/paired_i2v.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "generate_paired_fast_distilled"
    ]
    assert len(functions) == 1
    calls = [
        node for node in ast.walk(functions[0])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_paired_stage"
    ]
    second = [
        call for call in calls
        if any(
            key.arg == "stage_label"
            and isinstance(key.value, ast.Constant)
            and key.value.value == "stage2"
            for key in call.keywords
        )
    ]
    assert len(second) == 1
    sigma_args = [key.value for key in second[0].keywords if key.arg == "sigmas"]
    assert len(sigma_args) == 1
    assert isinstance(sigma_args[0], ast.Name)
    assert sigma_args[0].id == "stage2_sigmas"
    # A stage wrapper may be assigned to `stage2`; sigma must be a distinct
    # variable assigned from the exact upstream second-stage tensor.
    assignments = [
        node for node in ast.walk(functions[0])
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "stage2_sigmas"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assigned = assignments[0].value
    assert isinstance(assigned, ast.Call)
    assert isinstance(assigned.func, ast.Attribute)
    assert isinstance(assigned.func.value, ast.Name)
    assert assigned.func.value.id == "STAGE_2_DISTILLED_SIGMAS"
    assert assigned.func.attr == "to"
