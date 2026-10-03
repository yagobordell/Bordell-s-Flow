"""No-GPU regression gates for the *opt-in* shared-residency batch2 trial."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ai_video_factory.workers.ltx25.model import (
    LTX_PRODUCTION_I2V_PROFILE,
    DirectLTX25Backend,
    LTXVideoParameters,
)
from ai_video_factory.workers.ltx25.resource_metrics import memory_pressure


def _parameters(seed: int) -> LTXVideoParameters:
    return LTXVideoParameters(
        generation_profile=LTX_PRODUCTION_I2V_PROFILE,
        prompt="The subject turns their head.",
        seed=seed,
        width=1280,
        height=720,
        fps=24,
        num_frames=121,
    )


def test_gemma_build_once_but_forward_and_connector_remain_independent() -> None:
    # Lightweight CI omits the large GPU-only torch / LTX wheel set.
    # This mock parity test also runs on local LTX-enabled dev environments.
    pytest.importorskip("torch")
    from ai_video_factory.workers.ltx25.paired_i2v import PairInput, _encode_pair_prompts

    calls = {"gemma_builds": 0, "processor_builds": 0, "prompts": []}

    class Gemma:
        def encode(self, prompts):
            assert len(prompts) == 1, "Batching two prompts changes reference tokenization"
            calls["prompts"].append(tuple(prompts))
            return [(f"hidden-{prompts[0]}", f"mask-{prompts[0]}")]

    class Processor:
        def process_hidden_states(self, hidden, mask):
            assert mask == hidden.replace("hidden-", "mask-")
            return (hidden, mask)

    def gemma_ctx():
        calls["gemma_builds"] += 1
        return nullcontext(Gemma())

    def processor_builder():
        calls["processor_builds"] += 1
        return Processor()

    encoder = SimpleNamespace(
        _text_encoder_ctx=gemma_ctx,
        _build_embeddings_processor=processor_builder,
        _alloc_trim_strategy=None,
    )
    pair = (
        PairInput("Prompt A", 5243, image=None),
        PairInput("Prompt B", 5244, image=None),
    )
    with patch(
        "ltx_pipelines.utils.gpu_model.gpu_model",
        side_effect=lambda processor, **kwargs: nullcontext(processor),
    ):
        contexts = _encode_pair_prompts(SimpleNamespace(prompt_encoder=encoder), pair)
    assert contexts == (
        ("hidden-Prompt A", "mask-Prompt A"),
        ("hidden-Prompt B", "mask-Prompt B"),
    )
    assert calls == {
        "gemma_builds": 1,
        "processor_builds": 1,
        "prompts": [("Prompt A",), ("Prompt B",)],
    }


def test_image_encoder_build_once_per_stage_with_two_b1_image_forwards() -> None:
    torch = pytest.importorskip("torch")
    from ltx_core.conditioning.types.latent_cond import VideoConditionByLatentIndex

    from ai_video_factory.workers.ltx25.paired_i2v import PairInput, _batch_conditionings

    image_calls = []

    class Conditioner:
        def __init__(self):
            self.builds = 0

        def __call__(self, work):
            self.builds += 1
            return work(object())

    def encode(*, images, video_encoder, **kwargs):
        assert len(images) == 1
        assert video_encoder is not None
        image_calls.append(images[0])
        return [
            VideoConditionByLatentIndex(
                torch.full((1, 2, 1, 2, 2), float(images[0])),
                strength=1.0, latent_idx=0,
            )
        ]

    conditioner = Conditioner()
    pipeline = SimpleNamespace(
        image_conditioner=conditioner, dtype=torch.bfloat16, device=torch.device("cpu")
    )
    with patch(
        "ai_video_factory.workers.ltx25.paired_i2v.combined_image_conditionings",
        side_effect=encode,
    ):
        result = _batch_conditionings(
            pipeline,
            (PairInput("A", 5243, 7), PairInput("B", 5244, 9)),
            640, 384,
        )
    assert conditioner.builds == 1
    assert image_calls == [7, 9]
    assert len(result) == 1
    assert result[0].latent.shape[0] == 2
    assert result[0].latent[:, 0, 0, 0, 0].tolist() == [7.0, 9.0]


def test_shared_stage_uses_exact_same_transformer_for_both_passes() -> None:
    torch = pytest.importorskip("torch")
    import ai_video_factory.workers.ltx25.paired_i2v as paired
    from ai_video_factory.workers.ltx25.paired_i2v import (
        PairInput,
        generate_paired_fast_distilled,
    )

    model = object()
    first_image_conditioning = object()
    second_image_conditioning = object()
    image_conditionings = Mock(
        side_effect=[[first_image_conditioning], [second_image_conditioning]]
    )

    def checked_stage_context(**kwargs):
        # Both independent image encodes must finish before holding the GPU
        # transformer, minimizing additional VRAM at the VAE stage boundary.
        assert image_conditionings.call_count == 2
        return nullcontext(model)

    stage_context = Mock(side_effect=checked_stage_context)
    stage = SimpleNamespace(
        _model_wrapper=None, _is_streaming=False, _transformer_ctx=stage_context,
    )
    images = (PairInput("A", 5243, "a.png"), PairInput("B", 5244, "b.png"))
    context = SimpleNamespace(
        video_encoding=torch.zeros((1, 4, 2)),
        audio_encoding=torch.zeros((1, 4, 2)),
    )
    pipeline = SimpleNamespace(
        stage=stage, device=torch.device("cpu"),
        duration_predictor=None,
        image_conditioner=SimpleNamespace(resolve_crf=lambda images: images),
        video_decoder=SimpleNamespace(
            checkpoint_path="fake", diffvae_optimization=None,
        ),
        upsampler=lambda x: x,
        use_ancestral_sampler=True,
    )
    forwarded = []
    image_inputs_seen = []
    latent = torch.ones((2, 1, 1, 2, 2), dtype=torch.bfloat16)

    def fake_stage(*args, **kwargs):
        forwarded.append(kwargs["shared_transformer"])
        image_inputs_seen.append(kwargs["conditionings"])
        return SimpleNamespace(latent=latent), SimpleNamespace(latent=latent)

    with (
        patch.object(paired, "_encode_pair_prompts", return_value=(context, context)),
        patch.object(paired, "_batch_conditionings", image_conditionings),
        patch.object(paired, "_paired_stage", side_effect=fake_stage),
        patch.object(paired, "assert_resolution"),
        patch.object(paired, "require_num_frames_source"),
        patch.object(paired, "resolve_num_frames", return_value=121),
        patch.object(paired, "tiling_scale_factors_for_vae"),
        patch.object(paired, "ensure_tiling_config", return_value=None),
        patch.object(paired, "_memory_pressure", return_value={}),
    ):
        outputs = generate_paired_fast_distilled(
            pipeline,
            pair=images, width=1280, height=768,
            frame_rate=24, num_frames=121,
            reuse_transformer_across_stages=True,
        )
    assert len(outputs) == 2
    assert stage_context.call_count == 1
    assert forwarded == [model, model]
    assert image_conditionings.call_count == 2
    assert image_inputs_seen == [[first_image_conditioning], [second_image_conditioning]]


def test_two_build_batch2_does_not_preencode_second_stage() -> None:
    """The memory fallback must restore the visual-reference stage ordering."""
    torch = pytest.importorskip("torch")
    import ai_video_factory.workers.ltx25.paired_i2v as paired
    from ai_video_factory.workers.ltx25.paired_i2v import (
        PairInput,
        generate_paired_fast_distilled,
    )

    events = []
    latent = torch.ones((2, 1, 1, 2, 2), dtype=torch.bfloat16)
    context = SimpleNamespace(
        video_encoding=torch.zeros((1, 4, 2)),
        audio_encoding=torch.zeros((1, 4, 2)),
    )

    def conditioning(_pipeline, _pair, width, _height):
        events.append(("conditioning", width))
        return [width]

    def stage(*args, **kwargs):
        events.append(("stage", kwargs["stage_label"]))
        return SimpleNamespace(latent=latent), SimpleNamespace(latent=latent)

    def upsample(x):
        events.append(("upsample", None))
        return x

    pipeline = SimpleNamespace(
        stage=SimpleNamespace(),
        device=torch.device("cpu"),
        duration_predictor=None,
        image_conditioner=SimpleNamespace(resolve_crf=lambda images: images),
        video_decoder=SimpleNamespace(checkpoint_path="fake", diffvae_optimization=None),
        upsampler=upsample,
        use_ancestral_sampler=True,
    )
    with (
        patch.object(paired, "_encode_pair_prompts", return_value=(context, context)),
        patch.object(paired, "_batch_conditionings", side_effect=conditioning),
        patch.object(paired, "_paired_stage", side_effect=stage),
        patch.object(paired, "assert_resolution"),
        patch.object(paired, "require_num_frames_source"),
        patch.object(paired, "resolve_num_frames", return_value=121),
        patch.object(paired, "tiling_scale_factors_for_vae"),
        patch.object(paired, "ensure_tiling_config", return_value=None),
        patch.object(paired, "_memory_pressure", return_value={}),
    ):
        generate_paired_fast_distilled(
            pipeline,
            pair=(PairInput("A", 5243, "a.png"), PairInput("B", 5244, "b.png")),
            width=1280, height=768, frame_rate=24, num_frames=121,
            reuse_transformer_across_stages=False,
        )
    assert events == [
        ("conditioning", 640),
        ("stage", "stage1"),
        ("upsample", None),
        ("upsample", None),
        ("conditioning", 1280),
        ("stage", "stage2"),
    ]


def test_stage_reuse_oom_retries_approved_true_b2_before_any_b1(tmp_path: Path) -> None:
    backend = object.__new__(DirectLTX25Backend)
    backend._pair_attempt = Mock(side_effect=[RuntimeError("CUDA out of memory"), 26])
    backend.invalidate_pipeline = Mock()
    backend.generate = Mock()
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    active_exceptions_during_cleanup = []

    def inspect_cleanup():
        active_exceptions_during_cleanup.append(sys.exc_info()[0])

    with patch(
        "ai_video_factory.inference.gpu_failures.cleanup_cuda_memory",
        side_effect=inspect_cleanup,
    ):
        report = backend.generate_pair(
            keyframe_a=first, keyframe_b=second,
            output_a=tmp_path / "a.mp4", output_b=tmp_path / "b.mp4",
            parameters_a=_parameters(5243), parameters_b=_parameters(5244),
            reuse_transformer_across_stages=True,
            allow_sequential_fallback=False,
        )
    assert active_exceptions_during_cleanup == [None]
    assert report["mode"] == "batch2"
    assert report["actual_gpu_batch_size"] == 2
    assert report["shared_transformer_stage_builds"] == 2
    assert report["transformer_reused_between_stages"] is False
    assert len(report["failed_attempts"]) == 1
    assert report["failed_attempts"][0]["shared_between_stages"] is True
    assert [
        (item.kwargs["forward_batch_size"], item.kwargs["reuse_transformer_across_stages"])
        for item in backend._pair_attempt.call_args_list
    ] == [(2, True), (2, False)]
    backend.generate.assert_not_called()


def test_stage_reuse_disabled_is_unmodified_two_stage_b2(tmp_path: Path) -> None:
    backend = object.__new__(DirectLTX25Backend)
    backend._pair_attempt = Mock(return_value=27)
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    report = backend.generate_pair(
        keyframe_a=first, keyframe_b=second,
        output_a=tmp_path / "a.mp4", output_b=tmp_path / "b.mp4",
        parameters_a=_parameters(5243), parameters_b=_parameters(5244),
        reuse_transformer_across_stages=False,
    )
    assert report["mode"] == "batch2"
    assert report["shared_transformer_stage_builds"] == 2
    assert backend._pair_attempt.call_count == 1
    assert backend._pair_attempt.call_args.kwargs["reuse_transformer_across_stages"] is False


def test_memory_report_uses_cgroup_v2_and_process_counters(tmp_path: Path) -> None:
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir()
    cg.mkdir()
    (proc / "cgroup").write_text("0::/\n")
    (proc / "status").write_text("Name:\ttest\nVmRSS:\t1234 kB\nVmHWM:\t2345 kB\n")
    (proc / "stat").write_text(
        "123 (filename with spaces) " + " ".join(["S"] + ["0"] * 6 + ["12", "0", "34"]),
    )
    (proc / "io").write_text("read_bytes: 7654321\nrchar: 111\n")
    (cg / "memory.current").write_text("78912345\n")
    (cg / "memory.stat").write_text("pgmajfault 42\nfile 12345\nanon 67890\n")
    result = memory_pressure(proc_root=proc, cgroup_root=cg)
    assert result["process_available"] is True
    assert result["process_vmrss_bytes"] == 1234 * 1024
    assert result["process_read_bytes"] == 7654321
    assert result["process_minor_page_faults"] == 12
    assert result["process_major_page_faults"] == 34
    assert result["cgroup_available"] is True
    assert result["cgroup_memory_current_bytes"] == 78912345
    assert result["cgroup_pgmajfault"] == 42
    assert result["cgroup_file"] == 12345


def test_memory_report_falls_back_to_proc_if_cgroup_hidden(tmp_path: Path) -> None:
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir()
    cg.mkdir()
    (proc / "status").write_text("VmRSS:\t666 kB\n")
    result = memory_pressure(proc_root=proc, cgroup_root=cg)
    assert result["cgroup_available"] is False
    assert result["process_available"] is True
    assert result["process_vmrss_bytes"] == 666 * 1024
