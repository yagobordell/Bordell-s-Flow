"""Offline gates for the isolated experimental B=2 I2V task.

Only a real pinned RTX 5090 test can approve final visual quality. These
tests guard original recipe/seeds, atomic sidecars, fallback isolation and
local preflight WITHOUT buying GPU time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.workers.ltx25.experimental_batch2 import (
    LTX_I2V_BATCH2_TASK,
    Batch2Parameters,
    ExperimentalBatch2TaskRunner,
)
from ai_video_factory.workers.ltx25.model import (
    LTX_PRODUCTION_I2V_PROFILE,
    DirectLTX25Backend,
    LTXVideoParameters,
)

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "smoke" / "run_ltx25_i2v_batch2_trial.ps1"
CLIENT = ROOT / "scripts" / "smoke" / "submit_ltx25_i2v_batch2_smoke.py"


def parameters(seed: int, *, width: int = 1280, height: int = 720) -> dict:
    return {
        "generation_profile": LTX_PRODUCTION_I2V_PROFILE,
        "prompt": "The subject takes one small step.",
        "seed": seed, "width": width, "height": height,
        "fps": 24, "num_frames": 121,
    }


def request(*, bad_sidecar: bool = False) -> InferenceJobRequest:
    job_id = "ltx-batch2-unit-01"
    params = {"a": parameters(5243), "b": parameters(5244)}
    return InferenceJobRequest(
        job_id=job_id,
        task=LTX_I2V_BATCH2_TASK,
        inputs=[
            ObjectInput(name=name, key=f"test/{name}.png", sha256="a" * 64)
            for name in ("keyframe_a", "keyframe_b")
        ],
        output=ObjectOutput(
            key=f"jobs/{job_id}/batch2-a.mp4", content_type="video/mp4",
        ),
        sidecar_outputs={
            "video_b": ObjectOutput(
                key=f"jobs/{job_id}/batch2-b.mp4", content_type="video/mp4",
            ),
            "report": ObjectOutput(
                key=f"jobs/{job_id}/report.json",
                content_type="video/mp4" if bad_sidecar else "application/json",
            ),
        },
        parameters=params,
        max_attempts=1,
    )


def test_batch2_validation_rejects_profile_geometry_and_seed_drift() -> None:
    valid = Batch2Parameters.model_validate(request().parameters)
    assert (valid.a.seed, valid.b.seed) == (5243, 5244)
    assert valid.allow_sequential_fallback is False
    assert valid.reuse_transformer_across_stages is False

    bad = {"a": parameters(5243), "b": parameters(5243)}
    with pytest.raises(ValueError, match="distinct|seeds"):
        Batch2Parameters.model_validate(bad)
    bad["b"] = {**parameters(5244, width=1024), "height": 768}
    with pytest.raises(ValueError, match="restricted|720p|matching"):
        Batch2Parameters.model_validate(bad)
    bad["b"] = {**parameters(5244), "generation_profile": "ltx25-not-a-profile"}
    with pytest.raises(ValueError):
        Batch2Parameters.model_validate(bad)


def test_full_hd_batch2_requires_explicit_gpu_risk_without_any_hidden_fallback() -> None:
    full_hd = {
        "a": parameters(5243, width=1920, height=1080),
        "b": parameters(5244, width=1920, height=1080),
    }
    with pytest.raises(ValueError, match="explicitly accept"):
        Batch2Parameters.model_validate(full_hd)
    accepted = Batch2Parameters.model_validate({
        **full_hd, "accept_full_hd_batch2_oom_risk": True,
    })
    assert accepted.a.width == 1920
    assert accepted.a.height == 1080
    assert accepted.allow_sequential_fallback is False
    assert accepted.reuse_transformer_across_stages is False
    assert accepted.full_hd_stage2_mode == "cpu"
    assert Batch2Parameters.model_validate({
        **full_hd, "accept_full_hd_batch2_oom_risk": True,
        "full_hd_stage2_mode": "disk",
    }).full_hd_stage2_mode == "disk"
    with pytest.raises(ValueError, match="cpu or disk"):
        Batch2Parameters.model_validate({
            **full_hd, "accept_full_hd_batch2_oom_risk": True,
            "full_hd_stage2_mode": "gpu",
        })
    for unsafe in (
        {"allow_sequential_fallback": True},
        {"reuse_transformer_across_stages": True},
    ):
        with pytest.raises(ValueError, match="forbids"):
            Batch2Parameters.model_validate({
                **full_hd, "accept_full_hd_batch2_oom_risk": True, **unsafe,
            })
    with pytest.raises(ValueError, match="matching"):
        Batch2Parameters.model_validate({
            **full_hd, "b": parameters(5244), "accept_full_hd_batch2_oom_risk": True,
        })


def test_full_hd_batch2_backend_only_admits_true_parallel_with_consent(tmp_path: Path) -> None:
    backend = object.__new__(DirectLTX25Backend)
    backend._pair_attempt = Mock(return_value=27)
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    job = dict(
        keyframe_a=first, keyframe_b=second,
        output_a=tmp_path / "a.mp4", output_b=tmp_path / "b.mp4",
        parameters_a=LTXVideoParameters.model_validate(
            parameters(5243, width=1920, height=1080)
        ),
        parameters_b=LTXVideoParameters.model_validate(
            parameters(5244, width=1920, height=1080)
        ),
    )
    with pytest.raises(ValueError, match="explicitly accept"):
        backend.generate_pair(**job)
    backend._pair_attempt.assert_not_called()
    report = backend.generate_pair(**job, accept_full_hd_batch2_oom_risk=True)
    assert (report["requested_width"], report["requested_height"]) == (1920, 1080)
    assert report["actual_gpu_batch_size"] == 2
    assert report["mode"] == "batch2"
    assert report["transformer_offload_mode"] == "stage1:none,stage2:cpu"
    assert report["full_hd_gpu_validation_pending"] is True
    backend._pair_attempt.assert_called_once()
    assert backend._pair_attempt.call_args.kwargs["forward_batch_size"] == 2
    assert backend._pair_attempt.call_args.kwargs["full_hd_stage2_mode"] == "cpu"


def test_batch2_outputs_are_one_atomic_primary_plus_two_named_sidecars(tmp_path: Path) -> None:
    class FakeBackend:
        def generate_pair(self, **kwargs):
            assert kwargs["keyframe_a"].name == "image-a.png"
            assert kwargs["keyframe_b"].name == "image-b.png"
            assert kwargs["parameters_a"].seed == 5243
            assert kwargs["parameters_b"].seed == 5244
            kwargs["output_a"].write_bytes(b"video-a")
            kwargs["output_b"].write_bytes(b"video-b")
            return {"mode": "batch2", "quality": "visual_review_pending"}

    inputs = {
        "keyframe_a": tmp_path / "image-a.png",
        "keyframe_b": tmp_path / "image-b.png",
    }
    for path in inputs.values():
        path.write_bytes(b"dummy")
    backend = FakeBackend()
    runner = ExperimentalBatch2TaskRunner(backend=backend)
    result = runner.run(request(), inputs, tmp_path)
    assert result.path.read_bytes() == b"video-a"
    assert {sidecar.name for sidecar in result.sidecars} == {"video_b", "report"}
    assert next(x for x in result.sidecars if x.name == "video_b").path.read_bytes() == b"video-b"
    report = next(x for x in result.sidecars if x.name == "report")
    assert json.loads(report.path.read_text())["mode"] == "batch2"


def test_invalid_sidecars_are_rejected_before_gpu_work(tmp_path: Path) -> None:
    backend = Mock()
    runner = ExperimentalBatch2TaskRunner(backend=backend)
    inputs = {
        "keyframe_a": tmp_path / "a.png",
        "keyframe_b": tmp_path / "b.png",
    }
    with pytest.raises(ValueError, match="content types"):
        runner.run(request(bad_sidecar=True), inputs, tmp_path)
    backend.generate_pair.assert_not_called()


def test_hybrid_pinned_ram_resources_are_isolated_to_i2v() -> None:
    manifest = json.loads((ROOT / "deploy/salad/services.json").read_text())
    assert manifest["services"]["ltx25_i2v"]["resources"]["memory"] >= 65536
    assert manifest["services"]["ltx25"]["resources"]["memory"] == 40960
    script = SCRIPT.read_text(encoding="utf-8")
    assert '$FullHdStage2Mode -eq "cpu"' in script
    assert "65536" in script
    assert '"--full-hd-stage2-mode", $FullHdStage2Mode' in script


def test_full_hd_cli_preflight_is_free_and_requires_explicit_risk(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "full-hd-a.png", tmp_path / "full-hd-b.png"
    Image.new("RGB", (1920, 1080), (110, 120, 130)).save(first)
    Image.new("RGB", (1920, 1080), (140, 150, 160)).save(second)
    cmd = [
        sys.executable, str(CLIENT),
        "--image-a", str(first), "--prompt-a", "A small step",
        "--image-b", str(second), "--prompt-b", "A slow head turn",
        "--width", "1920", "--height", "1080",
        "--run-id", "full-hd-free-preflight",
        "--output-dir", str(tmp_path / "never-publish"),
        "--check-inputs-only",
    ]
    refused = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    assert refused.returncode != 0
    assert "accept-full-hd-batch2-oom-risk" in refused.stdout + refused.stderr
    allowed = subprocess.run(
        [*cmd, "--accept-full-hd-batch2-oom-risk"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "1920x1080" in allowed.stdout
    assert not (tmp_path / "never-publish").exists()


def test_gpu_oom_downgrades_to_shared_stage_without_hiding_the_mode(tmp_path: Path) -> None:
    backend = object.__new__(DirectLTX25Backend)
    backend._pair_attempt = Mock(side_effect=[RuntimeError("CUDA out of memory"), 27])
    backend.invalidate_pipeline = Mock()
    backend.generate = Mock()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    with patch("ai_video_factory.inference.gpu_failures.cleanup_cuda_memory"):
        report = backend.generate_pair(
            keyframe_a=a, keyframe_b=b,
            output_a=tmp_path / "a.mp4", output_b=tmp_path / "b.mp4",
            parameters_a=LTXVideoParameters.model_validate(parameters(5243)),
            parameters_b=LTXVideoParameters.model_validate(parameters(5244)),
            allow_sequential_fallback=True,
        )
    assert report["mode"] == "stage_shared_sequential_forward"
    assert report["actual_gpu_batch_size"] == 1
    assert report["shared_transformer_stage_builds"] == 2
    assert report["failed_attempts"][0]["gpu_failure"] == "oom"
    assert [x.kwargs["forward_batch_size"] for x in backend._pair_attempt.call_args_list] == [2, 1]
    backend.generate.assert_not_called()


def test_gpu_oom_last_resort_uses_exact_two_reference_generations(tmp_path: Path) -> None:
    backend = object.__new__(DirectLTX25Backend)
    backend._pair_attempt = Mock(side_effect=RuntimeError("CUDA out of memory"))
    backend.invalidate_pipeline = Mock()
    backend.generate = Mock()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    with patch("ai_video_factory.inference.gpu_failures.cleanup_cuda_memory"):
        report = backend.generate_pair(
            keyframe_a=a, keyframe_b=b,
            output_a=tmp_path / "a.mp4", output_b=tmp_path / "b.mp4",
            parameters_a=LTXVideoParameters.model_validate(parameters(5243)),
            parameters_b=LTXVideoParameters.model_validate(parameters(5244)),
            allow_sequential_fallback=True,
        )
    assert report["mode"] == "reference_sequential_oom_fallback"
    assert backend._pair_attempt.call_count == 2
    assert [x.kwargs["parameters"].seed for x in backend.generate.call_args_list] == [5243, 5244]


def test_gpu_batch2_is_never_silently_relabelled_after_oom(tmp_path: Path) -> None:
    backend = object.__new__(DirectLTX25Backend)
    backend._pair_attempt = Mock(side_effect=RuntimeError("CUDA out of memory"))
    backend.invalidate_pipeline = Mock()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    with patch("ai_video_factory.inference.gpu_failures.cleanup_cuda_memory"):
        with pytest.raises(RuntimeError, match="out of memory"):
            backend.generate_pair(
                keyframe_a=a, keyframe_b=b,
                output_a=tmp_path / "a.mp4", output_b=tmp_path / "b.mp4",
                parameters_a=LTXVideoParameters.model_validate(parameters(5243)),
                parameters_b=LTXVideoParameters.model_validate(parameters(5244)),
            )
    assert backend._pair_attempt.call_count == 1


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is not available")
def test_free_powershell_preflight_validates_real_images_and_env(tmp_path: Path) -> None:
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (1280, 720), (110, 120, 130)).save(first)
    Image.new("RGB", (1280, 720), (140, 150, 160)).save(second)
    env = tmp_path / "batch2-test.env"
    env.write_text("BATCH2_PREFLIGHT_TEST=1\n", encoding="utf-8")
    manifest = json.loads((ROOT / "deploy" / "salad" / "services.json").read_text())
    assert (
        manifest["services"]["ltx25_i2v"]["environment"]["LTX_EXPERIMENTAL_I2V_BATCH2_ENABLED"]
        == "true"
    )
    repo = manifest["services"]["ltx25_i2v"]["image"].rsplit(":", 1)[0]
    cmd = [
        "pwsh", "-NoProfile", "-File", str(SCRIPT),
        "-ImageA", str(first), "-PromptA", "Subject steps forward",
        "-ImageB", str(second), "-PromptB", "Subject turns head",
        "-ExpectedPinnedImage", f"{repo}@sha256:{'0' * 64}",
        "-EnvFile", str(env), "-CheckInputsOnly",
    ]
    valid = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False)
    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert "No Salad/R2 operations occurred" in valid.stdout
    Image.new("RGB", (1671, 941)).save(second)
    wrong = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False)
    assert wrong.returncode != 0
    assert "1280x720" in wrong.stdout + wrong.stderr
