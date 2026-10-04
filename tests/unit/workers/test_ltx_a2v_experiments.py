"""Offline A2V experimental-profile contracts; real lip-sync still requires GPU review."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _ltx_a2v_support import _seed_model_files, make_a2v_bindings
from PIL import Image

import ai_video_factory.workers.ltx25.a2v as a2v
from ai_video_factory.workers.ltx25.a2v_prompt_metrics import profile_a2v_prompt_encoder
from ai_video_factory.workers.ltx25.i2v_metrics import I2VPhaseMetrics


@pytest.mark.parametrize(
    ("profile", "offload", "decoder", "is_static"),
    [
        (a2v.LTX_A2V_BENCH_CPU_PROFILE, "cpu", "eager", False),
        (a2v.LTX_A2V_BENCH_NONE_PROFILE, "none", "eager", False),
        (a2v.LTX_A2V_PRODUCTION_FAST_PROFILE, "none", "eager", False),
        (a2v.LTX_A2V_BENCH_CPU_TRITON_PROFILE, "cpu", "triton", False),
        (a2v.LTX_A2V_BENCH_NONE_TRITON_PROFILE, "none", "triton", False),
        (a2v.LTX_A2V_BENCH_STATIC_CPU_PROFILE, "cpu", "eager", True),
        (a2v.LTX_A2V_BENCH_STATIC_NONE_PROFILE, "none", "eager", True),
    ],
)
def test_experiments_keep_the_original_audio_and_visual_recipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    offload: str,
    decoder: str,
    is_static: bool,
) -> None:
    root = tmp_path / "models"
    _seed_model_files(root)
    image = tmp_path / "avatar.png"
    Image.new("RGB", (1280, 736)).save(image)
    input_audio = tmp_path / "speech.wav"
    input_audio.write_bytes(b"source")
    conditioned = tmp_path / "conditioned.wav"
    conditioned.write_bytes(b"padded")
    state: dict[str, Any] = {
        "builds": 0, "calls": [], "encodes": [], "conditionings": [],
        "inference_depth": 0, "inference_entries": 0,
    }
    bindings = replace(
        make_a2v_bindings(state),
        offload_mode=SimpleNamespace(CPU="cpu", DISK="disk", NONE="none"),
        triton_probe=lambda: True,
    )
    monkeypatch.setattr(a2v, "_load_a2v_bindings", lambda: bindings)
    monkeypatch.setattr(
        a2v, "probe_audio",
        lambda _: a2v.AudioProbe("pcm_s16le", 24_000, 1, 4.0, sample_count=96_000),
    )
    plan = a2v.ReferenceAudioPlan(
        path=conditioned,
        decoded_probe=a2v.AudioProbe("pcm_s16le", 24_000, 2, 4.0, sample_count=96_000),
        conditioning_probe=a2v.AudioProbe(
            "pcm_s16le", 24_000, 2, 97 / 24, sample_count=97_000
        ),
        num_frames=97,
        grid_video_duration_seconds=97 / 24,
        padding_samples=1_000,
    )
    monkeypatch.setattr(
        a2v, "_prepare_reference_pipeline_audio", lambda *args, **kwargs: plan
    )
    monkeypatch.setattr(a2v, "_output_duration", lambda _: 97 / 24)
    backend = a2v.DirectLTX25AudioToVideoBackend(model_root=root)
    backend.prepare()
    params = a2v.LTXAudioToVideoParameters(
        generation_profile=profile, prompt="Unchanged phoneme prompt.", seed=4242
    )

    first = backend.generate(
        image_path=image, audio_path=input_audio,
        output_path=tmp_path / "first.mp4", parameters=params,
    )
    warm = backend.generate(
        image_path=image, audio_path=input_audio,
        output_path=tmp_path / "warm.mp4", parameters=params,
    )
    assert first["pipeline_reused"] is False
    assert warm["pipeline_reused"] is True
    assert state["builds"] == 1
    assert state["pipeline_init"]["offload_mode"] == offload
    assert first["offload_mode"] == offload
    assert first["quantization"] == "fp8_cast"
    assert first["diffvae_backend_requested"] == decoder
    assert first["diffvae_backend_effective"] == decoder
    assert first["stage_1_steps"] == 8
    assert first["stage_2_steps"] == 3
    assert first["stage_1_sampler"] == "euler_ancestral"
    assert first["stage_2_sampler"] == "euler"
    assert first["audio_frozen_stage_1"] is True
    assert first["audio_frozen_stage_2"] is True
    assert first["audio_padding_samples"] == 1_000
    assert first["conditioning_audio_samples"] == 97_000
    assert first["num_frames"] == 97
    assert first["fps"] == 24
    assert first["width"] == 1280
    assert first["height"] == 720
    assert first["seed"] == 4242
    assert "prompt_encode_seconds" not in first  # Fake pipeline has no Gemma stages
    assert "audio_prepare_seconds" in first
    assert "pipeline_build_seconds" in first
    assert first["negative_prompt_applied"] is is_static
    assert first["stage_1_image_strength"] == (0.8 if is_static else 0.7)
    assert "negative_prompt" in state["calls"][0] if is_static else (
        "negative_prompt" not in state["calls"][0]
    )
    assert state["calls"][0]["metrics"] is not None
    assert state["calls"][0]["audio_path"] == str(conditioned.resolve())
    assert state["encodes"][0]["audio"] is not None
    assert (tmp_path / "first.mp4").read_bytes() == (tmp_path / "warm.mp4").read_bytes()


def test_triton_unavailable_is_reported_instead_of_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_model_files(tmp_path)
    state: dict[str, Any] = {
        "builds": 0, "calls": [], "encodes": [], "conditionings": [],
        "inference_depth": 0, "inference_entries": 0,
    }
    bindings = replace(make_a2v_bindings(state), triton_probe=lambda: False)
    backend = a2v.DirectLTX25AudioToVideoBackend(model_root=tmp_path)
    backend._bindings = bindings
    backend._pipeline_params = bindings.detect_params("")
    with bindings.torch.inference_mode():
        backend._get_or_build_pipeline(
            bindings, generation_profile=a2v.LTX_A2V_BENCH_CPU_TRITON_PROFILE
        )
    assert backend._pipeline_effective_backend == "eager"


def test_prompt_profiler_restores_methods_and_propagates_failures() -> None:
    class Context:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_: Any) -> bool:
            return False

    class Encoder:
        def _text_encoder_ctx(self) -> Context:
            return Context()

        def _build_embeddings_processor(self) -> str:
            return "unchanged"

    encoder = Encoder()
    metrics = I2VPhaseMetrics(SimpleNamespace(cuda=None), "cpu")
    with pytest.raises(ValueError, match="deliberate"):
        with profile_a2v_prompt_encoder(encoder, metrics):
            with encoder._text_encoder_ctx():
                assert encoder._build_embeddings_processor() == "unchanged"
                raise ValueError("deliberate")
    assert "_text_encoder_ctx" not in vars(encoder)
    assert "_build_embeddings_processor" not in vars(encoder)
    assert encoder._build_embeddings_processor() == "unchanged"
    assert "prompt_model_acquire_seconds" in metrics.timings
    assert "prompt_model_release_seconds" in metrics.timings
    assert "prompt_embeddings_processor_build_seconds" in metrics.timings


def test_default_profiles_are_not_reassigned() -> None:
    assert a2v.LTXAudioToVideoParameters().generation_profile == a2v.LTX_A2V_PRODUCTION_FAST_PROFILE
    assert a2v.LTX_A2V_PRODUCTION_FAST_PROFILE not in a2v.LTX_A2V_EXPERIMENTS
    assert a2v.LTX_A2V_RESIDENCY_PROFILES[a2v.LTX_A2V_PRODUCTION_FAST_PROFILE] == ("none", "eager")
    assert a2v.LTX_A2V_REFERENCE_GENERATION_PROFILE not in a2v.LTX_A2V_EXPERIMENTS
    assert a2v.LTX_A2V_STATIC_SAFE_GENERATION_PROFILE not in a2v.LTX_A2V_EXPERIMENTS
