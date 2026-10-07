"""Offline regression coverage for experimental LTX I2V optimization profiles."""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.workers.ltx25 import model as ltx_model
from ai_video_factory.workers.ltx25.i2v_metrics import I2VPhaseMetrics
from ai_video_factory.workers.ltx25.jobs import ltx_video_application_job_id
from ai_video_factory.workers.ltx25.model import (
    LTX_CFG_BATCH2_PROFILE,
    LTX_CFG_BATCH2_SILENT_PROFILE,
    LTX_CFG_BATCH2_TRITON_PROFILE,
    LTX_DEFAULT_SILENT_VERIFY_PROFILE,
    LTX_FAST_VANILLA_PROFILE,
    LTX_FULLRES_NO_OFFLOAD_PROFILES,
    LTX_GENERATION_PROFILE,
    LTX_PRODUCTION_I2V_PROFILE,
    LTX_SUPPORTED_I2V_PROFILES,
    DirectLTX25Backend,
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
        LTX_PRODUCTION_I2V_PROFILE,
        LTX_DEFAULT_SILENT_VERIFY_PROFILE,
        LTX_CFG_BATCH2_PROFILE,
        LTX_CFG_BATCH2_SILENT_PROFILE,
        LTX_CFG_BATCH2_TRITON_PROFILE,
    )
    identities = {ltx_video_application_job_id(**request, generation_profile=p) for p in profiles}
    assert len(identities) == len(profiles)
    assert ltx_video_application_job_id(**request) == ltx_video_application_job_id(
        **request, generation_profile=LTX_PRODUCTION_I2V_PROFILE
    )
    assert ltx_video_application_job_id(**request) != ltx_video_application_job_id(
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


def test_production_i2v_accepts_public_1080p_on_padded_model_grid() -> None:
    parameters = LTXVideoParameters(
        generation_profile=LTX_PRODUCTION_I2V_PROFILE,
        prompt="Animate this frame naturally.",
        seed=4242,
        width=1920,
        height=1080,
        fps=24,
        num_frames=121,
    )

    assert LTX_PRODUCTION_I2V_PROFILE == LTX_FAST_VANILLA_PROFILE
    assert LTX_PRODUCTION_I2V_PROFILE not in LTX_FULLRES_NO_OFFLOAD_PROFILES
    assert ltx_model._pipeline_dimensions(parameters) == (1920, 1088)


def test_diffvae_uses_triton_only_with_probe_and_never_enables_natten() -> None:
    apply = SimpleNamespace(
        natten_available=lambda: True,
        triton_na_available=lambda: True,
    )
    def probe() -> bool:
        return True
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



class _PeakTrackingCuda:
    """Fake CUDA allocator that retains an earlier job's peak until reset."""

    def __init__(self) -> None:
        self.current = 80 * 1024 * 1024
        self.peak = 120 * 1024 * 1024
        self.reset_calls = 0

    def is_available(self) -> bool:
        return True

    def synchronize(self) -> None:
        pass

    def reset_peak_memory_stats(self, device: str) -> None:
        assert device == "cuda"
        self.reset_calls += 1
        self.peak = self.current

    def max_memory_allocated(self) -> int:
        return self.peak

    def max_memory_reserved(self) -> int:
        return self.peak


def test_cuda_peak_is_independent_for_each_generation() -> None:
    cuda = _PeakTrackingCuda()
    metrics = I2VPhaseMetrics(SimpleNamespace(cuda=cuda), "cuda")
    metrics.begin_generation()
    assert metrics.cuda_peak_reset is True
    assert cuda.reset_calls == 1
    assert metrics.cuda_peak()["cuda_peak_allocated_mb"] == 80.0

    cuda.peak = 94 * 1024 * 1024
    assert metrics.cuda_peak()["cuda_peak_reserved_mb"] == 94.0

    # Starting the next clip resets peaks again instead of inheriting 94 MiB.
    next_run = I2VPhaseMetrics(SimpleNamespace(cuda=cuda), "cuda")
    next_run.begin_generation()
    assert cuda.reset_calls == 2
    assert next_run.cuda_peak()["cuda_peak_allocated_mb"] == 80.0


def test_cuda_peak_instrumentation_failure_never_fails_generation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cuda = _PeakTrackingCuda()

    def unavailable(device: str) -> None:
        raise RuntimeError(f"peak reset unsupported: {device}")

    cuda.reset_peak_memory_stats = unavailable  # type: ignore[method-assign]
    metrics = I2VPhaseMetrics(SimpleNamespace(cuda=cuda), "cuda")
    with caplog.at_level(logging.WARNING):
        metrics.begin_generation()
    assert metrics.cuda_peak_reset is False
    assert "LTX25_I2V_CUDA_PEAK_RESET_UNAVAILABLE" in caplog.text
    assert metrics.cuda_peak()["cuda_peak_allocated_mb"] == 120.0


def test_production_i2v_keeps_original_visual_recipe_without_unused_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Exercise the actual default backend adapter without external GPU services.

    The only optimization in a fresh production request is skipping audio
    after both visual sampling stages; CFG=1, seed, prompt, negative guidance,
    dimensions, frame count, eager DiffVAE and audio-free MP4 stay unchanged.
    """
    keyframe = tmp_path / "original.png"
    Image.new("RGB", (1280, 720)).save(keyframe)
    backend = DirectLTX25Backend(model_root=tmp_path, device="cpu")
    pipeline = object()
    backend._pipeline = pipeline
    backend._pipeline_requested_diffvae_backend = "eager"
    backend._pipeline_effective_diffvae_backend = "eager"
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
        inference_mode=nullcontext,
    )
    output_inputs: list[dict[str, object]] = []
    generator_calls: list[dict[str, object]] = []

    def fake_generator(actual_pipeline: object, **kwargs: object) -> SimpleNamespace:
        assert actual_pipeline is pipeline
        generator_calls.append(kwargs)
        return SimpleNamespace(
            video=iter([b"same-deterministic-visual-output"]),
            num_frames=kwargs["num_frames"],
            tiling_config=object(),
        )

    def fake_encode_video(**kwargs: object) -> None:
        output_inputs.append(kwargs)
        assert kwargs["audio"] is None
        frames = list(kwargs["video"])
        Path(str(kwargs["output_path"])).write_bytes(b"".join(frames))

    bindings = SimpleNamespace(
        torch=fake_torch,
        image_conditioning_input=lambda **kwargs: kwargs,
        encode_video=fake_encode_video,
        get_video_chunks_number=lambda *args: 1,
    )
    monkeypatch.setattr(backend, "_get_bindings", lambda: bindings)
    monkeypatch.setattr(backend, "_validate_runtime", lambda _: None)
    monkeypatch.setattr(
        backend,
        "_get_or_build_pipeline",
        lambda _bindings, requested_diffvae_backend: pipeline,
    )
    monkeypatch.setattr(ltx_model, "_pipeline_dimensions", lambda _: (1280, 720))
    monkeypatch.setattr(
        ltx_model,
        "_load_default_i2v_negative_guidance",
        lambda: (fake_generator, "the original direct-to-camera negative guidance"),
    )
    parameters = LTXVideoParameters(
        generation_profile=LTX_GENERATION_PROFILE,
        prompt="The character raises a cup while maintaining a steady camera.",
        seed=4243,
        width=1280,
        height=720,
        fps=24,
        num_frames=49,
    )
    first, second, verify = (
        tmp_path / "first.mp4", tmp_path / "second.mp4", tmp_path / "verify.mp4"
    )
    with caplog.at_level(logging.INFO):
        backend.generate(keyframe_path=keyframe, output_path=first, parameters=parameters)
        backend.generate(keyframe_path=keyframe, output_path=second, parameters=parameters)
        backend.generate(
            keyframe_path=keyframe,
            output_path=verify,
            parameters=parameters.model_copy(
                update={"generation_profile": LTX_DEFAULT_SILENT_VERIFY_PROFILE}
            ),
        )

    assert first.read_bytes() == second.read_bytes() == verify.read_bytes() == (
        b"same-deterministic-visual-output"
    )
    assert len(generator_calls) == len(output_inputs) == 3
    for call in generator_calls:
        assert call["prompt"] == parameters.prompt
        assert call["negative_prompt"] == "the original direct-to-camera negative guidance"
        assert call["seed"] == parameters.seed
        assert call["num_frames"] == parameters.num_frames
        assert call["width"] == parameters.width
        assert call["height"] == parameters.height
        assert call["stage_1_max_batch_size"] == 1
        assert call["decode_audio"] is False

    event_marker = "LTX25_I2V_PHASE_METRICS "
    events = [
        json.loads(rec.getMessage().split(event_marker, maxsplit=1)[1])
        for rec in caplog.records
        if event_marker in rec.getMessage()
    ]
    assert len(events) == 3
    assert [event["generation_profile"] for event in events] == [
        LTX_GENERATION_PROFILE, LTX_GENERATION_PROFILE,
        LTX_DEFAULT_SILENT_VERIFY_PROFILE,
    ]
    assert all(event["diffvae_backend_effective"] == "eager" for event in events)
    assert all(event["audio_decode_enabled"] is False for event in events)
    assert all(event["stage1_max_batch_size"] == 1 for event in events)
    # A prepared pipeline object does not imply model weights are warm.
    assert events[0]["pipeline_reused"] is True
    assert events[0]["pipeline_known_warm"] is False
    assert events[0]["pipeline_prior_successful_generations"] == 0
    assert events[1]["pipeline_known_warm"] is True
    assert events[1]["pipeline_prior_successful_generations"] == 1
    # The verification profile must exercise the exact same visual adapter.
    def visual_args(call: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in call.items() if key != "metrics"}

    assert visual_args(generator_calls[0]) == visual_args(generator_calls[2])
    assert events[2]["pipeline_prior_successful_generations"] == 2
    assert events[2]["stage1_max_batch_size"] == 1
    backend._release_pipeline_locked()
    assert backend._pipeline_successful_generations == 0


def test_audio_decoding_is_strictly_after_both_visual_diffusion_stages() -> None:
    # This order is the core no-visual-change safety condition: the audio
    # decoder must not be a denoising input, and the MP4 has audio=None.
    source = Path("src/ai_video_factory/workers/ltx25/static_i2v.py").read_text(
        encoding="utf-8"
    )
    stage_1 = source.index("denoiser=GuidedDenoiser(")
    stage_2 = source.index("denoiser=SimpleDenoiser(")
    video_decode = source.index("decoded_video = self.video_decoder(")
    audio_decode = source.index("decoded_audio = self.audio_decoder(")
    assert stage_1 < stage_2 < video_decode < audio_decode
    assert 'audio=ModalitySpec(context=audio_context)' in source
    assert "initial_latent=audio_state.latent" in source
    assert "audio=None," in Path(
        "src/ai_video_factory/workers/ltx25/model.py"
    ).read_text(encoding="utf-8")


def test_production_and_a2v_profiles_are_not_batch2_or_triton() -> None:
    model_source = Path("src/ai_video_factory/workers/ltx25/model.py").read_text(
        encoding="utf-8"
    )
    assert (
        "stage_1_batch = 2 if parameters.generation_profile "
        "in LTX_EXPERIMENTAL_PROFILES else 1"
    ) in model_source
    assert "decode_audio = parameters.generation_profile == LTX_CFG_BATCH2_PROFILE" in (
        model_source
    )
    a2v_source = Path("src/ai_video_factory/workers/ltx25/a2v.py").read_text(
        encoding="utf-8"
    )
    assert "audio" in a2v_source
