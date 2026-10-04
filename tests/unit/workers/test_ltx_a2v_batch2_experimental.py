"""CPU-only A2V paired task and strict preflight regressions, no paid GPU."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_PRODUCTION_FAST_PROFILE,
    LTX_A2V_RUN_STATIC_FAST_PROFILE,
    DirectLTX25AudioToVideoBackend,
    LTXAudioToVideoParameters,
)
from ai_video_factory.workers.ltx25.experimental_a2v_batch2 import (
    LTX_A2V_BATCH2_TASK,
    A2VBatch2Parameters,
    ExperimentalA2VBatch2TaskRunner,
)
from scripts.smoke import verify_ltx25_a2v_batch2_image as probe


def _params(seed: int, *, width: int = 1920, height: int = 1080) -> dict:
    return {
        "seed": seed, "width": width, "height": height, "fps": 24,
        "prompt": "Lips move naturally in exact sync with the supplied speech.",
        "generation_profile": LTX_A2V_RUN_STATIC_FAST_PROFILE,
    }


def test_pair_accepts_full_hd_but_rejects_unvalidated_recipe_or_shape() -> None:
    assert LTXAudioToVideoParameters().width == 1280
    assert LTXAudioToVideoParameters.model_validate(_params(7)).height == 1080
    valid = {
        "a": _params(7), "b": _params(8),
        "stage2_mode": "cpu", "accept_full_hd_oom_risk": True,
    }
    assert A2VBatch2Parameters.model_validate(valid).a.seed == 7
    assert A2VBatch2Parameters.model_validate(valid).vae_backend == "eager"
    assert A2VBatch2Parameters.model_validate(valid).guidance_forward_batch_size == 2
    assert A2VBatch2Parameters.model_validate({
        **valid, "guidance_forward_batch_size": 4,
    }).guidance_forward_batch_size == 4
    assert A2VBatch2Parameters.model_validate({
        **valid, "vae_backend": "triton_experimental",
    }).vae_backend == "triton_experimental"
    for mutation in (
        {"accept_full_hd_oom_risk": False},
        {"stage2_mode": "disk"},
        {"vae_backend": "unknown"},
        {"guidance_forward_batch_size": 3},
        {"b": _params(7)},
        {"b": _params(8, width=1280, height=720)},
        {"b": {**_params(8), "generation_profile": LTX_A2V_PRODUCTION_FAST_PROFILE}},
    ):
        with pytest.raises(ValidationError):
            A2VBatch2Parameters.model_validate({**valid, **mutation})


class FakeBackend:
    def __init__(self, *, batch: int = 2) -> None:
        self.calls: list[dict[str, Any]] = []
        self.invalidations = 0
        self.batch = batch

    def prepare(self) -> None:
        pass

    def ready(self) -> None:
        pass

    def invalidate_pipeline(self) -> None:
        self.invalidations += 1

    def generate_pair(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        for name in ("output_a", "output_b"):
            kwargs[name].write_bytes(b"valid-test-mp4")
        return {
            "mode": "batch2", "actual_gpu_batch_size": self.batch,
            "guidance_forward_batch_size": kwargs.get("guidance_forward_batch_size", 2),
            "video_vae_backend": (
                "triton" if kwargs.get("vae_backend") == "triton_experimental"
                else "eager"
            ),
        }


def _job() -> InferenceJobRequest:
    names = ("image_a", "audio_a", "image_b", "audio_b")
    job_id = "ltx-a2v-b2-unit-0001"
    return InferenceJobRequest(
        job_id=job_id, task=LTX_A2V_BATCH2_TASK,
        inputs=[
            ObjectInput(
                name=name, key=f"unit/{name}.bin", sha256=str(i + 1) * 64,
                content_type="image/png" if name.startswith("image") else "audio/wav",
            ) for i, name in enumerate(names)
        ],
        output=ObjectOutput(
            key=f"jobs/{job_id}/batch2-a.mp4", content_type="video/mp4",
        ),
        sidecar_outputs={
            "video_b": ObjectOutput(
                key=f"jobs/{job_id}/batch2-b.mp4", content_type="video/mp4",
            ),
            "report": ObjectOutput(
                key=f"jobs/{job_id}/batch2-report.json", content_type="application/json",
            ),
        },
        max_attempts=1,
        parameters=A2VBatch2Parameters.model_validate({
            "a": _params(7), "b": _params(8),
            "accept_full_hd_oom_risk": True,
        }).model_dump(),
    )


def test_atomic_four_input_contract_and_independent_audio(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = ExperimentalA2VBatch2TaskRunner(backend=backend)
    inputs = {
        key: tmp_path / f"{key}.bin"
        for key in ("image_a", "audio_a", "image_b", "audio_b")
    }
    for path in inputs.values():
        path.write_bytes(b"input")
    artifact = runner.run(_job(), inputs, tmp_path)
    assert artifact.path.read_bytes() == b"valid-test-mp4"
    assert {s.name for s in artifact.sidecars} == {"video_b", "report"}
    assert backend.calls[0]["audio_a"] != backend.calls[0]["audio_b"]
    assert backend.calls[0]["vae_backend"] == "eager"
    assert backend.calls[0]["guidance_forward_batch_size"] == 2
    assert backend.calls[0]["parameters_a"].seed == 7
    assert backend.calls[0]["parameters_b"].seed == 8
    assert json.loads((tmp_path / "batch2-report.json").read_text())["actual_gpu_batch_size"] == 2
    with pytest.raises(ValueError, match="image_a/audio_a"):
        runner.run(_job(), {k: v for k, v in inputs.items() if k != "audio_b"}, tmp_path)


def test_sequential_results_cannot_masquerade_as_gpu_batch2(tmp_path: Path) -> None:
    backend = FakeBackend(batch=1)
    runner = ExperimentalA2VBatch2TaskRunner(backend=backend)
    inputs = {
        name: tmp_path / name
        for name in ("image_a", "audio_a", "image_b", "audio_b")
    }
    with pytest.raises(RuntimeError, match="sequential fallback"):
        runner.run(_job(), inputs, tmp_path)


def test_audio_grid_mismatch_rejected_before_gpu_pipeline(tmp_path: Path) -> None:
    backend = object.__new__(DirectLTX25AudioToVideoBackend)
    backend._lock = __import__("threading").RLock()
    backend.ready = Mock(side_effect=AssertionError("GPU must not be touched"))
    probe_a = SimpleNamespace(duration_seconds=5.0)
    plan_a = SimpleNamespace(num_frames=121)
    plan_b = SimpleNamespace(num_frames=129)
    with (
        patch("ai_video_factory.workers.ltx25.a2v.probe_audio", return_value=probe_a),
        patch("ai_video_factory.workers.ltx25.a2v._validate_audio_duration"),
        patch("ai_video_factory.workers.ltx25.a2v._prepare_reference_pipeline_audio",
              side_effect=[plan_a, plan_b]),
    ):
        with pytest.raises(ValueError, match="matching audio-driven"):
            backend.generate_pair(
                image_a=tmp_path / "image_a.png", audio_a=tmp_path / "audio_a.wav",
                image_b=tmp_path / "image_b.png", audio_b=tmp_path / "audio_b.wav",
                output_a=tmp_path / "out_a.mp4", output_b=tmp_path / "out_b.mp4",
                parameters_a=LTXAudioToVideoParameters.model_validate(_params(7)),
                parameters_b=LTXAudioToVideoParameters.model_validate(_params(8)),
            )
    backend.ready.assert_not_called()


def test_offline_docker_probe_requires_exact_digest_and_aborts_on_bad_image() -> None:
    pinned = "docker.io/yagobordell/ai-video-factory@sha256:" + "a" * 64
    with patch.object(
        probe.subprocess, "run",
        return_value=SimpleNamespace(
            returncode=0, stdout="LTX25_A2V_B2_DOCKER_ABI_OK\n", stderr="",
        ),
    ) as run:
        probe.verify(pinned)
    args = run.call_args.args[0]
    assert "--gpus" not in args
    assert "--rm" in args
    assert "A2VBatch2Parameters" in args[-1]
    for bad in ("docker.io/x:latest", "docker.io/x@sha256:zz"):
        with pytest.raises(ValueError):
            probe.verify(bad)
    with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(
        returncode=1, stdout="", stderr="pinned builder missing",
    )):
        with pytest.raises(RuntimeError, match="never start paid Salad"):
            probe.verify(pinned)


def test_cli_contains_lipsync_and_four_input_gates() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "scripts/smoke/submit_ltx25_a2v_batch2_smoke.py").read_text()
    for marker in (
        "_validate_reference_voice_tail",
        "_validate_decodable_video",
        "audio_a", "audio_b",
        "num_frames", "max_attempts=1", "frames[0] != frames[1]",
    ):
        assert marker in source
    wrapper = (root / "scripts/smoke/run_ltx25_a2v_batch2_trial.ps1").read_text()
    assert wrapper.index("--check-inputs-only") < wrapper.index('-Action Start')
    assert "ltx25_a2v_experimental" in wrapper
