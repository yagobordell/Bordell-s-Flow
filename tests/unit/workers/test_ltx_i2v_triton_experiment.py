"""No-paid-GPU guards for experimental Full HD B2 Triton decoder selection."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ai_video_factory.workers.ltx25.experimental_batch2 import Batch2Parameters
from ai_video_factory.workers.ltx25.model import (
    LTX_PRODUCTION_I2V_PROFILE,
    DirectLTX25Backend,
    LTXVideoParameters,
    _select_diffvae_backend,
)
from ai_video_factory.workers.ltx25 import triton_na_probe
from scripts.smoke import compare_ltx25_i2v_backend_trial as comparison
from scripts.smoke import verify_ltx25_i2v_hybrid_image as abi

PINNED = "docker.io/yagobordell/ai-video-factory@sha256:" + "b" * 64
ROOT = Path(__file__).resolve().parents[3]


def _params(seed: int, width: int = 1920, height: int = 1080) -> dict:
    return {
        "generation_profile": LTX_PRODUCTION_I2V_PROFILE,
        "prompt": "The subject stands up and walks away.",
        "seed": seed,
        "width": width, "height": height,
        "fps": 24, "num_frames": 121,
    }


def test_triton_is_explicit_only_for_true_full_hd_cpu_b2() -> None:
    pair = {
        "a": _params(5243), "b": _params(5244),
        "accept_full_hd_batch2_oom_risk": True,
    }
    assert Batch2Parameters.model_validate(pair).vae_backend == "eager"
    assert Batch2Parameters.model_validate({
        **pair, "vae_backend": "triton_experimental",
    }).vae_backend == "triton_experimental"
    for unsafe in (
        {"a": _params(5243, 1280, 720), "b": _params(5244, 1280, 720)},
        {"full_hd_stage2_mode": "disk"},
        {"allow_sequential_fallback": True},
        {"reuse_transformer_across_stages": True},
    ):
        with pytest.raises(ValueError):
            Batch2Parameters.model_validate({
                **pair, "vae_backend": "triton_experimental", **unsafe,
            })


def test_triton_backend_never_uses_natten_or_silently_reverts_to_eager() -> None:
    diffvae = SimpleNamespace(
        natten_available=lambda: True,
        triton_na_available=lambda: False,
    )
    unavailable = SimpleNamespace(diffvae_apply=diffvae, triton_probe=lambda: False)
    assert _select_diffvae_backend(unavailable, "eager") == "eager"
    assert diffvae.natten_available() is False
    assert diffvae.triton_na_available() is False
    with pytest.raises(RuntimeError, match="refusing eager fallback"):
        _select_diffvae_backend(unavailable, "triton_strict")
    available = SimpleNamespace(diffvae_apply=diffvae, triton_probe=lambda: True)
    assert _select_diffvae_backend(available, "triton_strict") == "triton"
    assert diffvae.natten_available() is False
    assert diffvae.triton_na_available() is True
    assert _select_diffvae_backend(available, "eager") == "eager"
    assert diffvae.triton_na_available() is False


def test_cuda_child_probe_stops_on_crash_and_reports_elapsed() -> None:
    with patch.object(
        triton_na_probe.subprocess, "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="I2V_TRITON_NA_GPU_PROBE_OK { \"results\": [] }\n",
            stderr="",
        ),
    ) as run:
        assert triton_na_probe.ensure_triton_gpu_probe() >= 0
        assert run.call_args.args[0][1:] == [
            "-m", "ai_video_factory.workers.ltx25.triton_na_probe", "--gpu-probe",
        ]
        assert run.call_args.kwargs["timeout"] >= 60
    for result in (
        SimpleNamespace(returncode=139, stdout="", stderr="native SIGSEGV"),
        SimpleNamespace(returncode=0, stdout="missing marker", stderr=""),
    ):
        with patch.object(triton_na_probe.subprocess, "run", return_value=result):
            with pytest.raises(RuntimeError, match="FAILED"):
                triton_na_probe.ensure_triton_gpu_probe()
    with patch.object(
        triton_na_probe.subprocess, "run",
        side_effect=triton_na_probe.subprocess.TimeoutExpired("python", 180),
    ):
        with pytest.raises(RuntimeError, match="unavailable"):
            triton_na_probe.ensure_triton_gpu_probe()


def test_triton_pair_runs_probe_before_timed_inference_and_reports_backend(
    tmp_path: Path,
) -> None:
    backend = object.__new__(DirectLTX25Backend)
    backend._pair_attempt = Mock(return_value=100)
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    pair = {
        "keyframe_a": first, "keyframe_b": second,
        "output_a": tmp_path / "a.mp4", "output_b": tmp_path / "b.mp4",
        "parameters_a": LTXVideoParameters.model_validate(_params(5243)),
        "parameters_b": LTXVideoParameters.model_validate(_params(5244)),
        "accept_full_hd_batch2_oom_risk": True,
        "vae_backend": "triton_experimental",
    }
    with patch.object(triton_na_probe, "ensure_triton_gpu_probe", return_value=1.25) as probe:
        result = backend.generate_pair(**pair)
    probe.assert_called_once_with()
    assert result["vae_backend"] == "triton_experimental"
    assert result["triton_gpu_probe_seconds"] == 1.25
    assert result["actual_gpu_batch_size"] == 2
    assert backend._pair_attempt.call_args.kwargs["vae_backend"] == "triton_experimental"
    backend._pair_attempt.reset_mock()
    with patch.object(
        triton_na_probe, "ensure_triton_gpu_probe", side_effect=RuntimeError("child segfault")
    ):
        with pytest.raises(RuntimeError, match="child segfault"):
            backend.generate_pair(**pair)
    backend._pair_attempt.assert_not_called()


def test_exact_image_triton_probe_is_optional_and_fails_closed() -> None:
    with patch.object(
        abi.subprocess, "run",
        return_value=SimpleNamespace(
            returncode=0, stderr="",
            stdout="I2V_HYBRID_DOCKER_ABI_OK\nI2V_TRITON_DOCKER_ABI_OK\n",
        ),
    ) as call:
        abi.verify(PINNED, require_triton=True)
    source = call.call_args.args[0][-1]
    assert "triton_na3d" in source
    assert "ensure_triton_gpu_probe" in source
    assert "triton_strict" in source
    assert "--gpus" not in call.call_args.args[0]
    with patch.object(
        abi.subprocess, "run",
        return_value=SimpleNamespace(
            returncode=0, stdout="I2V_HYBRID_DOCKER_ABI_OK\n", stderr="",
        ),
    ):
        with pytest.raises(RuntimeError, match="Rebuild the image"):
            abi.verify(PINNED, require_triton=True)


def _baseline_pair(tmp_path: Path) -> tuple[Path, Path]:
    old, new = tmp_path / "old", tmp_path / "new"
    for directory in (old, new):
        directory.mkdir()
        data = {
            "inputs": {
                "inputs": [
                    {"name": "keyframe_a", "sha256": "a" * 64,
                     "prompt": "The subject stands up and walks away.", "seed": 5243},
                    {"name": "keyframe_b", "sha256": "b" * 64,
                     "prompt": "The subject stands up and walks away.", "seed": 5244},
                ],
                "video_width": 1920, "video_height": 1080,
                "full_hd_stage2_mode": "cpu",
            },
        }
        (directory / "reference-provenance.json").write_text(json.dumps(data))
        params = {
            "a": _params(5243), "b": _params(5244),
            "vae_backend": "eager" if directory == old else "triton_experimental",
            "allow_sequential_fallback": False,
            "reuse_transformer_across_stages": False,
        }
        (directory / "job-request.json").write_text(json.dumps({"parameters": params}))
        for suffix in ("a", "b"):
            (directory / f"batch2-{suffix}.mp4").write_bytes(b"stub")
    return old, new


def test_quality_comparison_rejects_input_mismatch_and_stays_review_pending(
    tmp_path: Path,
) -> None:
    baseline, candidate = _baseline_pair(tmp_path)
    assert comparison.verify_matched_inputs(baseline, candidate)["matching_inputs"]
    with (
        patch.object(comparison.shutil, "which", return_value="ffmpeg"),
        patch.object(
            comparison, "_ffmpeg_scores",
            return_value={"frames": 121, "average": 0.98},
        ) as scoring,
    ):
        result = comparison.compare(baseline, candidate)
    assert scoring.call_count == 4
    assert result["quality_status"] == "HUMAN_SIDE_BY_SIDE_REVIEW_REQUIRED"
    assert (candidate / "backend-quality-comparison.json").is_file()
    provenance = json.loads(
        (candidate / "reference-provenance.json").read_text(encoding="utf-8")
    )
    provenance["inputs"]["inputs"][0]["seed"] = 777
    (candidate / "reference-provenance.json").write_text(json.dumps(provenance))
    with pytest.raises(ValueError, match="inputs changed"):
        comparison.verify_matched_inputs(baseline, candidate)


def test_script_requires_triton_preflight_before_any_paid_start() -> None:
    source = (
        ROOT / "scripts/smoke/run_ltx25_i2v_batch2_trial.ps1"
    ).read_text(encoding="utf-8")
    assert "VaeBackend" in source
    assert "--require-triton" in source
    assert source.index("--require-triton") < source.index("$StartRequested = $false")
