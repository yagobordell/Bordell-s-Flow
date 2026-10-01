"""Offline regression coverage for experimental LTX I2V optimization profiles."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.workers.ltx25.i2v_metrics import I2VPhaseMetrics
from ai_video_factory.workers.ltx25.jobs import ltx_video_application_job_id
from ai_video_factory.workers.ltx25.model import (
    LTX_CFG_BATCH2_PROFILE,
    LTX_CFG_BATCH2_SILENT_PROFILE,
    LTX_CFG_BATCH2_TRITON_PROFILE,
    LTX_GENERATION_PROFILE,
    LTX_SUPPORTED_I2V_PROFILES,
    LTXVideoParameters,
    _select_diffvae_backend,
)


def test_experimental_profiles_are_distinct_and_keep_default_fingerprint() -> None:
    request = dict(
        shot_id=1,
        prompt="A person works silently beside a window.",
        keyframe_sha256="a" * 64,
        seed=4242,
        width=1280,
        height=720,
        fps=24,
        num_frames=121,
    )
    profiles = (
        LTX_GENERATION_PROFILE,
        LTX_CFG_BATCH2_PROFILE,
        LTX_CFG_BATCH2_SILENT_PROFILE,
        LTX_CFG_BATCH2_TRITON_PROFILE,
    )
    identities = {ltx_video_application_job_id(**request, generation_profile=p) for p in profiles}
    assert len(identities) == len(profiles)
    assert ltx_video_application_job_id(**request) == ltx_video_application_job_id(
        **request, generation_profile=LTX_GENERATION_PROFILE
    )
    for profile in LTX_SUPPORTED_I2V_PROFILES:
        assert LTXVideoParameters.model_validate(
            {
                "generation_profile": profile,
                "prompt": request["prompt"],
                "seed": request["seed"],
                "width": request["width"],
                "height": request["height"],
                "fps": request["fps"],
                "num_frames": request["num_frames"],
            }
        ).generation_profile == profile


def test_diffvae_uses_triton_only_with_probe_and_never_enables_natten() -> None:
    apply = SimpleNamespace(
        natten_available=lambda: True,
        triton_na_available=lambda: True,
    )
    probe = lambda: True
    bindings = SimpleNamespace(diffvae_apply=apply, triton_probe=probe)
    assert _select_diffvae_backend(bindings, "eager") == "eager"
    assert not apply.natten_available()
    assert not apply.triton_na_available()
    assert _select_diffvae_backend(bindings, "triton") == "triton"
    assert not apply.natten_available()
    assert apply.triton_na_available is probe


def test_triton_unavailable_falls_back_to_eager() -> None:
    apply = SimpleNamespace(natten_available=lambda: True, triton_na_available=lambda: True)
    bindings = SimpleNamespace(diffvae_apply=apply, triton_probe=lambda: False)
    assert _select_diffvae_backend(bindings, "triton") == "eager"
    assert not apply.natten_available()
    assert not apply.triton_na_available()


def test_lazy_video_decode_timing_is_measured_on_iteration_not_construction() -> None:
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    metrics = I2VPhaseMetrics(fake_torch, "cpu")
    produced = []

    def chunks():
        produced.append(1)
        yield "first"
        produced.append(2)
        yield "second"

    iterator = metrics.video_chunks(chunks())
    assert not produced
    assert "video_decode_seconds" not in metrics.timings
    assert list(iterator) == ["first", "second"]
    assert produced == [1, 2]
    assert metrics.timings["video_decode_seconds"] >= 0.0
    assert metrics.cuda_peak() == {}


def test_custom_generator_keeps_joint_diffusion_but_can_skip_only_final_audio() -> None:
    source = Path("src/ai_video_factory/workers/ltx25/static_i2v.py").read_text(
        encoding="utf-8"
    )
    assert "stage_1_max_batch_size: int = 1" in source
    assert '"max_batch_size": 2' in source
    assert 'audio=ModalitySpec(context=audio_context)' in source
    assert "if decode_audio:" in source
    assert "decoded_audio = self.audio_decoder(audio_state.latent)" in source
    assert 'metrics.timings["audio_decode_seconds"] = 0.0' in source


@pytest.mark.parametrize("profile", [LTX_CFG_BATCH2_PROFILE, LTX_CFG_BATCH2_SILENT_PROFILE])
def test_experimental_profiles_remain_explicit_not_the_default(profile: str) -> None:
    assert profile != LTX_GENERATION_PROFILE
