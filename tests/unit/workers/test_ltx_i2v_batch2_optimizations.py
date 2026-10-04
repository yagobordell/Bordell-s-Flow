"""No-GPU regression gates for the *opt-in* shared-residency batch2 trial."""

from __future__ import annotations

import sys
import threading
import weakref
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


def test_approved_per_prompt_gemma_disposal_restored_for_each_video() -> None:
    pytest.importorskip("torch")
    from ai_video_factory.workers.ltx25.paired_i2v import PairInput, _encode_pair_prompts

    events = []

    def resolve(images):
        assert len(images) == 1
        events.append(("resolve", images[0]))
        return ["resolved-" + images[0][0]]

    def prompt_encoder(prompts, **kwargs):
        assert len(prompts) == 1
        assert kwargs["enhance_first_prompt"] is False
        assert kwargs["enhance_static_cache"] is False
        events.append(("prompt", prompts[0], kwargs["enhance_prompt_image"]))
        return ["context-for-" + prompts[0]]

    pipeline = SimpleNamespace(
        prompt_encoder=prompt_encoder,
        image_conditioner=SimpleNamespace(resolve_crf=resolve),
    )
    pair = (
        PairInput("Prompt A", 5243, image=("a.png",)),
        PairInput("Prompt B", 5244, image=("b.png",)),
    )
    with patch(
        "ai_video_factory.workers.ltx25.paired_i2v._memory_pressure",
        return_value={"process_available": True},
    ):
        prepared = _encode_pair_prompts(pipeline, pair)
    assert prepared[0] == (PairInput("Prompt A", 5243, "resolved-a.png"), "context-for-Prompt A")
    assert prepared[1] == (PairInput("Prompt B", 5244, "resolved-b.png"), "context-for-Prompt B")
    assert events == [
        ("resolve", ("a.png",)),
        ("prompt", "Prompt A", "a.png"),
        ("resolve", ("b.png",)),
        ("prompt", "Prompt B", "b.png"),
    ]


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
        patch.object(paired, "_encode_pair_prompts", return_value=(
            (images[0], context), (images[1], context),
        )),
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
        patch.object(paired, "_encode_pair_prompts", return_value=(
            (PairInput("A", 5243, "a.png"), context),
            (PairInput("B", 5244, "b.png"), context),
        )),
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


def test_full_hd_releases_stage1_image_conditioning_before_second_stage() -> None:
    """Retained stage1 conditioning must not steal 1080p transformer VRAM."""
    torch = pytest.importorskip("torch")
    import ai_video_factory.workers.ltx25.paired_i2v as paired

    class Conditioning:
        def __init__(self, value: int) -> None:
            self.value = value
            self.latent = torch.ones((2, 3), dtype=torch.bfloat16)

    marker = []
    events = []
    context = SimpleNamespace(
        video_encoding=torch.zeros((1, 4, 2)),
        audio_encoding=torch.zeros((1, 4, 2)),
    )
    pair = (paired.PairInput("A", 5243, "a.png"), paired.PairInput("B", 5244, "b.png"))

    def conditioning(_pipeline, _pair, width: int, _height: int):
        events.append(("condition", width))
        if width > 1000:
            import gc

            gc.collect()
            assert marker[0]() is None, "Unused stage1 GPU conditioning remains alive"
        item = Conditioning(width)
        if width < 1000:
            marker.append(weakref.ref(item))
        return [item]

    def stage(*args, **kwargs):
        events.append(("stage", kwargs["stage_label"]))
        latent = torch.ones((2, 1, 1, 2, 2), dtype=torch.bfloat16)
        return SimpleNamespace(latent=latent), SimpleNamespace(latent=latent)

    pipeline = SimpleNamespace(
        stage=SimpleNamespace(),
        device=torch.device("cpu"),
        duration_predictor=None,
        video_decoder=SimpleNamespace(checkpoint_path="fake", diffvae_optimization=None),
        upsampler=lambda x: x,
        use_ancestral_sampler=True,
    )
    with (
        patch.object(paired, "_encode_pair_prompts", return_value=(
            (pair[0], context), (pair[1], context),
        )),
        patch.object(paired, "_batch_conditionings", side_effect=conditioning),
        patch.object(paired, "_paired_stage", side_effect=stage),
        patch.object(paired, "assert_resolution"),
        patch.object(paired, "require_num_frames_source"),
        patch.object(paired, "resolve_num_frames", return_value=121),
        patch.object(paired, "tiling_scale_factors_for_vae"),
        patch.object(paired, "ensure_tiling_config", return_value=None),
        patch.object(paired, "_log_gpu_checkpoint"),
    ):
        result = paired.generate_paired_fast_distilled(
            pipeline, pair=pair, width=1920, height=1088,
            frame_rate=24, num_frames=121,
            full_hd_memory_cleanup=True,
        )
    assert len(result) == 2
    assert events == [
        ("condition", 960), ("stage", "stage1"),
        ("condition", 1920), ("stage", "stage2"),
    ]


def test_hybrid_full_hd_stage2_factory_runs_after_stage1_and_conditioning() -> None:
    torch = pytest.importorskip("torch")
    import ai_video_factory.workers.ltx25.paired_i2v as paired

    events = []
    latent = torch.ones((2, 1, 1, 2, 2), dtype=torch.bfloat16)
    context = SimpleNamespace(
        video_encoding=torch.zeros((1, 4, 2)),
        audio_encoding=torch.zeros((1, 4, 2)),
    )
    pair = (paired.PairInput("A", 5243, "a.png"), paired.PairInput("B", 5244, "b.png"))
    stage1 = object()
    stage2 = object()

    def encode(_pipeline, _pair, width, _height):
        events.append(("conditioning", width))
        return [width]

    def stage(stage_arg, **kwargs):
        events.append(("stage", kwargs["stage_label"]))
        assert stage_arg is (stage1 if kwargs["stage_label"] == "stage1" else stage2)
        if kwargs["stage_label"] == "stage2":
            assert isinstance(kwargs["sigmas"], torch.Tensor)
            assert torch.equal(
                kwargs["sigmas"],
                paired.STAGE_2_DISTILLED_SIGMAS.to(dtype=torch.float32),
            ), "A lazy stage2 DiffusionStage must never overwrite the sigma tensor"
        return SimpleNamespace(latent=latent), SimpleNamespace(latent=latent)

    def factory():
        # The GPU-resident transformer and image encoder must have finished
        # before loading the pinned-CPU transformer block weights.
        assert events == [
            ("conditioning", 960), ("stage", "stage1"),
            ("upsample", None), ("upsample", None),
            ("conditioning", 1920),
        ]
        events.append(("construct_stage2", None))
        return stage2

    def upsample(video):
        events.append(("upsample", None))
        return video

    pipeline = SimpleNamespace(
        stage=stage1, device=torch.device("cpu"), duration_predictor=None,
        video_decoder=SimpleNamespace(checkpoint_path="fake", diffvae_optimization=None),
        upsampler=upsample, use_ancestral_sampler=True,
    )
    with (
        patch.object(paired, "_encode_pair_prompts", return_value=(
            (pair[0], context), (pair[1], context),
        )),
        patch.object(paired, "_batch_conditionings", side_effect=encode),
        patch.object(paired, "_paired_stage", side_effect=stage),
        patch.object(paired, "assert_resolution"),
        patch.object(paired, "require_num_frames_source"),
        patch.object(paired, "resolve_num_frames", return_value=121),
        patch.object(paired, "tiling_scale_factors_for_vae"),
        patch.object(paired, "ensure_tiling_config", return_value=None),
        patch.object(paired, "_log_gpu_checkpoint"),
    ):
        result = paired.generate_paired_fast_distilled(
            pipeline, pair=pair, width=1920, height=1088,
            frame_rate=24, num_frames=121,
            full_hd_memory_cleanup=True,
            stage2_factory=factory,
        )
    assert len(result) == 2
    assert events[-2:] == [
        ("construct_stage2", None), ("stage", "stage2"),
    ]


def test_full_hd_boundary_trim_only_evicts_unreachable_cuda_cache() -> None:
    torch = pytest.importorskip("torch")
    import ai_video_factory.workers.ltx25.paired_i2v as paired

    with (
        patch.object(paired.gc, "collect") as collect,
        patch.object(torch.cuda, "is_available", return_value=True),
        patch.object(torch.cuda, "empty_cache") as empty,
        patch.object(paired, "_cuda_memory_snapshot", return_value={}),
        patch.object(paired, "_memory_pressure", return_value={}),
    ):
        paired._log_gpu_checkpoint("baseline", trim=False)
        collect.assert_not_called()
        empty.assert_not_called()
        paired._log_gpu_checkpoint("full_hd_stage_boundary", trim=True)
        collect.assert_called_once_with()
        empty.assert_called_once_with()


def test_full_hd_pair_uses_gpu_stage1_and_lazy_cpu_or_disk_stage2(
    tmp_path: Path,
) -> None:
    """The B2 forward remains two-video; no second stage loads before called."""
    import ai_video_factory.workers.ltx25.model as model

    backend = object.__new__(DirectLTX25Backend)
    backend._lock = threading.RLock()
    backend._device = "cuda"
    backend._pipeline_successful_generations = 0
    src_a, src_b = tmp_path / "a.png", tmp_path / "b.png"
    src_a.write_bytes(b"a")
    src_b.write_bytes(b"b")
    calls = []
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            reset_peak_memory_stats=lambda: None,
            max_memory_allocated=lambda: 27,
        ),
    )
    fake_bindings = SimpleNamespace(
        torch=fake_torch,
        image_conditioning_input=lambda **kwargs: kwargs,
        encode_video=lambda **kwargs: calls.append(kwargs),
        get_video_chunks_number=lambda *_args: 1,
    )
    stage2 = object()

    def generate(_pipeline, **kwargs):
        factory = kwargs["stage2_factory"]
        if factory is not None:
            assert factory() is stage2
        return (
            SimpleNamespace(video="a", num_frames=121, tiling_config=None),
            SimpleNamespace(video="b", num_frames=121, tiling_config=None),
        )

    fake_pair_module = SimpleNamespace(
        PairInput=lambda prompt, seed, image: (prompt, seed, image),
        generate_paired_fast_distilled=Mock(side_effect=generate),
    )
    orig_pair_module = sys.modules.get("ai_video_factory.workers.ltx25.paired_i2v")
    fake_name = "ai_video_factory.workers.ltx25.paired_i2v"
    try:
        sys.modules[fake_name] = fake_pair_module
        for width, height, stage2_mode in (
            (1280, 720, None), (1920, 1080, "cpu"), (1920, 1080, "disk"),
        ):
            pipeline = SimpleNamespace(stage=object())
            with (
                patch.object(backend, "ready"),
                patch.object(backend, "_get_bindings", return_value=fake_bindings),
                patch.object(backend, "_validate_runtime"),
                patch.object(backend, "_get_or_build_pipeline", return_value=pipeline) as loader,
                patch.object(
                    backend, "_full_hd_streaming_stage2", return_value=stage2,
                ) as alternate,
                patch.object(model, "_torch_inference_context", return_value=nullcontext()),
                patch.object(model, "_prepare_grid_keyframe", side_effect=lambda p, *_a, **_kw: p),
                patch.object(model, "_crop_video_to_requested", side_effect=lambda v, **_kw: v),
            ):
                first = LTXVideoParameters(
                    generation_profile=LTX_PRODUCTION_I2V_PROFILE,
                    prompt="A", seed=5243, width=width, height=height,
                    fps=24, num_frames=121,
                )
                second = first.model_copy(update={"prompt": "B", "seed": 5244})
                assert backend._pair_attempt(
                    keyframe_a=src_a, keyframe_b=src_b,
                    output_a=tmp_path / "a.mp4", output_b=tmp_path / "b.mp4",
                    parameters_a=first, parameters_b=second,
                    forward_batch_size=2,
                    full_hd_stage2_mode=stage2_mode or "cpu",
                ) == 27
                assert loader.call_args.kwargs["requested_offload_mode"] == "none"
                kwargs = fake_pair_module.generate_paired_fast_distilled.call_args.kwargs
                assert kwargs["max_forward_batch_size"] == 2
                assert kwargs["full_hd_memory_cleanup"] is (stage2_mode is not None)
                if stage2_mode is None:
                    assert kwargs["stage2_factory"] is None
                    alternate.assert_not_called()
                else:
                    alternate.assert_called_once_with(
                        pipeline, fake_bindings, mode=stage2_mode,
                    )
            fake_pair_module.generate_paired_fast_distilled.reset_mock()
    finally:
        if orig_pair_module is None:
            sys.modules.pop(fake_name, None)
        else:
            sys.modules[fake_name] = orig_pair_module
    assert len(calls) == 6


def test_hybrid_stage2_build_reuses_identical_fp8_policy_and_cpu_slots() -> None:
    backend = object.__new__(DirectLTX25Backend)
    backend._model_files = SimpleNamespace(transformer=Path("/fake/model.safetensors"))
    policy = SimpleNamespace(fuse_rule=object())
    seen = []

    class Stage:
        _is_streaming = False
        _quantization = policy
        _dtype = "bf16"
        _device = "cuda"
        _alloc_trim_strategy = "trim"
        video_scale_factors = "unchanged"

        @classmethod
        def from_checkpoint(cls, **kwargs):
            seen.append(kwargs)
            mode = kwargs["offload_mode"]
            slots = None if mode == "cpu_enum" else 2
            builder = SimpleNamespace(
                cpu_slots_count=slots,
                registry=SimpleNamespace(_cache_weights=False),
                _i2v_progressive_pinned_blocks_supported=True,
            )
            return SimpleNamespace(
                _is_streaming=True,
                _transformer_builder=builder,
            )

    bindings = SimpleNamespace(
        offload_mode=SimpleNamespace(CPU="cpu_enum", DISK="disk_enum"),
    )
    pipeline = SimpleNamespace(stage=Stage())
    for mode in ("cpu", "disk"):
        result = backend._full_hd_streaming_stage2(
            pipeline, bindings, mode=mode,
        )
        assert result._is_streaming
        assert seen[-1]["quantization"] is policy
        assert seen[-1]["checkpoint_path"] == "/fake/model.safetensors"
        assert seen[-1]["scale_factors"] == "unchanged"
        assert result._transformer_builder.cpu_slots_count == (
            None if mode == "cpu" else 2
        )
        assert getattr(result._transformer_builder, "_i2v_progressive_pinned_blocks", False) is (
            mode == "cpu"
        )


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
