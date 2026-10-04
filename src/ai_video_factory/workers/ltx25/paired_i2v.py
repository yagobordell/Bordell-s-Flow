"""EXPERIMENTAL two-video LTX 2.5 FP8 inference on ONE shared transformer.

A single B=2 forward is attempted per denoising step (8+3 steps). The
original per-video prompt, image conditioning, noise generators, sampler,
two-stage sigmas, FP8 quantization, VAE and output geometry are retained.
This module is *never* selected by the existing single-video task.
"""

from __future__ import annotations

import gc
import json
import logging
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from functools import partial
from typing import Any

import torch
from ltx_core.batch_split import BatchSplitAdapter
from ltx_core.components.patchifiers import AudioPatchifier, VideoLatentPatchifier
from ltx_core.conditioning.types.latent_cond import VideoConditionByLatentIndex
from ltx_core.model.video_vae import AUTO_TILING
from ltx_core.tools import AudioLatentTools, VideoLatentTools
from ltx_core.types import AudioLatentShape, VideoLatentShape, VideoPixelShape
from ltx_pipelines.utils.blocks import require_num_frames_source, resolve_num_frames
from ltx_pipelines.utils.constants import DISTILLED_SIGMAS, STAGE_2_DISTILLED_SIGMAS
from ltx_pipelines.utils.denoisers import SimpleDenoiser
from ltx_pipelines.utils.helpers import (
    assert_resolution,
    combined_image_conditionings,
    create_noised_state,
    ensure_tiling_config,
    tiling_scale_factors_for_vae,
)
from ltx_pipelines.utils.samplers import euler_ancestral_denoising_loop, euler_denoising_loop

from .resource_metrics import cuda_memory_snapshot as _cuda_memory_snapshot
from .resource_metrics import memory_pressure as _memory_pressure

logger = logging.getLogger(__name__)


def _log_gpu_checkpoint(phase: str, *, trim: bool = False) -> None:
    """Record true CUDA allocator pressure at B=2 stage boundaries.

    In Full HD only, a boundary trim releases CACHED memory once no longer
    reachable stage-1 tensors have been discarded; it does not evict live
    tensors, modify latents, sampler arithmetic or offload model weights.
    """
    if trim:
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    logger.info(
        "LTX25_I2V_PAIR_GPU_CHECKPOINT %s",
        json.dumps({
            "phase": phase,
            "trimmed_unused_cache": trim,
            "cuda": _cuda_memory_snapshot(),
            "system": _memory_pressure(),
        }, sort_keys=True),
    )


class _ProfiledBatchTransformer(torch.nn.Module):
    """Read-only per-forward diagnostics around the *unchanged* true B=2 model."""

    def __init__(self, model: torch.nn.Module, stage_label: str) -> None:
        super().__init__()
        self.model = model
        self.stage_label = stage_label
        self.forwards = 0

    @property
    def num_blocks(self) -> int:
        return self.model.num_blocks

    def forward(self, video: Any, audio: Any, perturbations: Any) -> Any:
        self.forwards += 1
        prefix = f"{self.stage_label}_forward_{self.forwards}"
        _log_gpu_checkpoint(prefix + "_before")
        try:
            output = self.model(
                video=video, audio=audio, perturbations=perturbations,
            )
        except Exception:
            _log_gpu_checkpoint(prefix + "_failed")
            raise
        _log_gpu_checkpoint(prefix + "_after")
        return output


def _encode_pair_prompts(pipeline: Any, pair: tuple[PairInput, PairInput]) -> tuple[Any, Any]:
    """Restore approved B=2 per-sample image resolution and prompt memory lifetime.

    Per sample: resolve CRF, call the complete upstream PromptEncoder so Gemma
    and its embeddings processor dispose/trim, then move to the next sample.
    The shared-Gemma optimization OOMed during model-weight copy on Salad.
    """
    prepared = []
    for index, sample in enumerate(pair):
        started = time.perf_counter()
        image = pipeline.image_conditioner.resolve_crf([sample.image])[0]
        logger.info(
            "LTX25_I2V_PAIR_PROMPT_BASELINE_START index=%d memory=%s",
            index, json.dumps(_memory_pressure(), sort_keys=True),
        )
        (context,) = pipeline.prompt_encoder(
            [sample.prompt],
            enhance_first_prompt=False,
            enhance_static_cache=False,
            enhance_prompt_image=sample.image[0],
        )
        prepared.append((PairInput(sample.prompt, sample.seed, image), context))
        logger.info(
            "LTX25_I2V_PAIR_PROMPT_BASELINE_METRICS %s",
            json.dumps({
                "index": index,
                "separate_text_encoder_build": True,
                "elapsed_seconds": time.perf_counter() - started,
                "memory": _memory_pressure(),
            }, sort_keys=True),
        )
    return prepared[0], prepared[1]


@dataclass(frozen=True)
class PairInput:
    prompt: str
    seed: int
    image: Any


class _IndependentGaussianNoiser:
    """Draw exactly the B=1 reference random stream for each member, video then audio."""

    def __init__(self, seeds: tuple[int, int], device: Any):
        self.generators = tuple(
            torch.Generator(device=device).manual_seed(seed) for seed in seeds
        )

    def __call__(self, state: Any, noise_scale: float = 1.0) -> Any:
        from dataclasses import replace

        noise = torch.cat(
            [
                torch.randn(
                    state.latent[i : i + 1].shape,
                    generator=generator,
                    dtype=state.latent.dtype,
                    device=state.latent.device,
                )
                for i, generator in enumerate(self.generators)
            ],
            dim=0,
        )
        latent = torch.lerp(state.latent.float(), noise.float(), noise_scale)
        latent = torch.lerp(state.clean_latent.float(), latent, state.denoise_mask)
        return replace(state, latent=latent.to(state.latent.dtype))


def _ancestral_noise(seeds: tuple[int, int], device: Any):
    # LTX 2.5 ancestral sampler uses its own independent seed +10000 stream,
    # separate from GaussianNoiser's initial video/audio noise.
    generators = tuple(
        torch.Generator(device=device).manual_seed(seed + 10000) for seed in seeds
    )

    def draw(x: torch.Tensor, _unused_generator: Any) -> torch.Tensor:
        if x.shape[0] != 2:
            raise ValueError("Paired ancestral noise requires exactly two latents")
        return torch.cat(
            [
                torch.randn(
                    x[i : i + 1].shape,
                    generator=generator,
                    dtype=x.dtype,
                    device=x.device,
                )
                for i, generator in enumerate(generators)
            ],
            dim=0,
        )

    return draw


def _paired_stage(
    stage: Any,
    *,
    video_context: torch.Tensor,
    audio_context: torch.Tensor,
    conditionings: list[Any],
    noiser: _IndependentGaussianNoiser,
    seeds: tuple[int, int],
    sigmas: torch.Tensor,
    width: int,
    height: int,
    frames: int,
    fps: float,
    initial_video: torch.Tensor | None = None,
    initial_audio: torch.Tensor | None = None,
    ancestral: bool = False,
    max_forward_batch_size: int = 2,
    stage_label: str = "unknown",
    shared_transformer: Any | None = None,
    monitor_full_hd: bool = False,
) -> tuple[Any, Any]:
    """Pinned DiffusionStage.__call__ semantics with VideoPixelShape(batch=2).

    Only B=2 state construction and independent per-sample RNG depart from
    upstream's B=1 implementation. The transformer is built once per stage.
    """
    from ltx_core.components.diffusion_steps import EulerAncestralDiffusionStep

    if video_context.shape[0] != 2 or audio_context.shape[0] != 2:
        raise ValueError("Paired prompt encoder must return two independent contexts")
    pixel_shape = VideoPixelShape(batch=2, frames=frames, height=height, width=width, fps=fps)
    video_tools = VideoLatentTools(
        VideoLatentPatchifier(patch_size=1),
        VideoLatentShape.from_pixel_shape(pixel_shape, scale_factors=stage.video_scale_factors),
        fps,
        scale_factors=stage.video_scale_factors,
    )
    audio_tools = AudioLatentTools(
        AudioPatchifier(patch_size=1), AudioLatentShape.from_video_pixel_shape(pixel_shape)
    )
    video_state = create_noised_state(
        video_tools, conditionings, noiser, stage._dtype, stage._device,
        noise_scale=float(sigmas[0]), initial_latent=initial_video,
    )
    audio_state = create_noised_state(
        audio_tools, [], noiser, stage._dtype, stage._device,
        noise_scale=float(sigmas[0]), initial_latent=initial_audio,
    )
    loop: Any = euler_denoising_loop
    stepper: Any = None
    if ancestral:
        stepper = EulerAncestralDiffusionStep(eta=1.0, s_noise=1.0)
        loop = partial(
            euler_ancestral_denoising_loop,
            noise_seed=seeds[0] + 10000,
            new_noise_fn=_ancestral_noise(seeds, stage._device),
            model_dtype=stage._dtype,
        )
    memory_before = _memory_pressure()
    build_started = time.perf_counter()
    stage_ctx = (
        nullcontext(shared_transformer)
        if shared_transformer is not None
        else stage._transformer_ctx(video_tools=video_tools)
    )
    with stage_ctx as built:
        build_seconds = (
            0.0 if shared_transformer is not None
            else time.perf_counter() - build_started
        )
        denoise_started = time.perf_counter()
        transformer = (
            built if stage._model_wrapper is None
            else stage._model_wrapper(built, video_tools)
        )
        # This is genuinely a single B=2 model forward, NOT two serialized jobs.
        batched_transformer = BatchSplitAdapter(transformer, max_batch_size=max_forward_batch_size)
        if monitor_full_hd:
            batched_transformer = _ProfiledBatchTransformer(
                batched_transformer, stage_label,
            )
        denoiser = SimpleDenoiser(video_context, audio_context)
        kwargs = {
            "sigmas": sigmas,
            "video_state": video_state,
            "audio_state": audio_state,
            "transformer": batched_transformer,
            "denoiser": denoiser,
        }
        if stepper is not None:
            kwargs["stepper"] = stepper
        else:
            from ltx_core.components.diffusion_steps import EulerDiffusionStep
            kwargs["stepper"] = EulerDiffusionStep()
        video_state, audio_state = loop(**kwargs)
        denoise_seconds = time.perf_counter() - denoise_started
        video_state = video_tools.unpatchify(video_tools.clear_conditioning(video_state))
        audio_state = audio_tools.unpatchify(audio_tools.clear_conditioning(audio_state))
    logger.info(
        "LTX25_I2V_PAIR_STAGE_METRICS %s",
        json.dumps({
            "stage": stage_label,
            "forward_batch": max_forward_batch_size,
            "transformer_build_seconds": build_seconds,
            "shared_transformer": shared_transformer is not None,
            "denoise_seconds": denoise_seconds,
            "memory_before": memory_before,
            "memory_after": _memory_pressure(),
        }, sort_keys=True),
    )
    return video_state, audio_state


def _batch_conditionings(pipeline: Any, pair: tuple[PairInput, PairInput], width: int, height: int):
    """Encode each reference image separately to preserve the original conditioning."""
    def encode_individually(encoder: Any) -> list[Any]:
        # Same one-image forward as the approved result, on the same encoder
        # instance. A single encoder build per stage instead of two.
        prepared = []
        for item in pair:
            encoded = combined_image_conditionings(
                images=[item.image], height=height, width=width,
                video_encoder=encoder, dtype=pipeline.dtype, device=pipeline.device,
            )
            if len(encoded) != 1 or not isinstance(encoded[0], VideoConditionByLatentIndex):
                raise ValueError("Experimental batch2 supports one first-frame image per sample")
            prepared.append(encoded[0])
        return prepared

    prepared = pipeline.image_conditioner(encode_individually)
    if prepared[0].strength != prepared[1].strength:
        raise ValueError("Experimental batch2 requires identical image strengths")
    return [
        VideoConditionByLatentIndex(
            latent=torch.cat([x.latent for x in prepared], dim=0),
            strength=prepared[0].strength, latent_idx=0,
        )
    ]


def _lazy_decode(pipeline: Any, latent: torch.Tensor, tiling: Any, generator: Any):
    # Build/consume the VAE sequentially. Two lazy iterators must not hold
    # competing decoder instances or contaminate each other's module state.
    yield from pipeline.video_decoder(latent, tiling, generator, dtype=torch.bfloat16)


def generate_paired_fast_distilled(
    pipeline: Any,
    *,
    pair: tuple[PairInput, PairInput],
    width: int,
    height: int,
    frame_rate: float,
    num_frames: int,
    max_forward_batch_size: int = 2,
    reuse_transformer_across_stages: bool = False,
    full_hd_memory_cleanup: bool = False,
    stage2_factory: Callable[[], Any] | None = None,
) -> tuple[Any, Any]:
    """Generate two clips in B=2 transformer forwards with B=1 quality semantics.

    Does not modify single-video production settings or its random streams.
    OOM and runtime errors are handled by the caller, never suppressed here.
    """
    from ltx_pipelines.utils.types import PipelineOutput

    if max_forward_batch_size not in (1, 2):
        raise ValueError("Experimental transformer forward batch size must be 1 or 2")
    if full_hd_memory_cleanup and (width, height) != (1920, 1088):
        raise ValueError("Full HD memory profile requires internal 1920x1088 model grid")
    if stage2_factory is not None and (not full_hd_memory_cleanup or reuse_transformer_across_stages):
        raise ValueError("Alternate stage2 requires separate Full HD stage residency")
    if len(pair) != 2 or pair[0].seed == pair[1].seed:
        raise ValueError("Batch2 needs two independent seeds")
    assert_resolution(height=height, width=width, is_two_stage=True)
    require_num_frames_source(num_frames, pipeline.duration_predictor)
    # Preserve per-video Gemma tokenization/padding by encoding ONE prompt at a
    # time. Attempting combined text encoding could change text-conditioning.
    # Explicitly restore upstream per-prompt Gemma and connector disposal.
    # The unverified shared-Gemma attempt OOMed while transferring weights.
    prepared = _encode_pair_prompts(pipeline, pair)
    resolved = tuple(x[0] for x in prepared)
    video_ctx = torch.cat([x[1].video_encoding for x in prepared], dim=0)
    audio_ctx = torch.cat([x[1].audio_encoding for x in prepared], dim=0)
    duration = tuple(
        resolve_num_frames(
            num_frames, pipeline.duration_predictor,
            video_encoding=x[1].video_encoding,
            audio_encoding=x[1].audio_encoding,
            frame_rate=frame_rate,
        )
        for x in prepared
    )
    if duration != (num_frames, num_frames):
        raise ValueError("Batch2 requires identical fixed frame counts")
    # The concatenated independent contexts are now the only tensors used by
    # denoising. Do not retain both individual GPU encoder outputs as well.
    del prepared
    _log_gpu_checkpoint("prompt_contexts_released")
    scale = tiling_scale_factors_for_vae(pipeline.video_decoder.checkpoint_path)
    tiling = ensure_tiling_config(
        AUTO_TILING, scale_factors=scale,
        vae_checkpoint_path=pipeline.video_decoder.checkpoint_path,
        video_shape=VideoPixelShape(1, num_frames, height, width, frame_rate),
        diffvae_optimization=pipeline.video_decoder.diffvae_optimization,
        device=pipeline.device,
    )
    # Both image conditionings depend solely on the two input frames. Resolve
    # them BEFORE acquiring the optional shared 22-GiB GPU transformer: the
    # VideoEncoder must not compete for VRAM with resident transformer weights.
    # Same two independent B=1 encodes, strengths and values as before.
    stage1_images = _batch_conditionings(pipeline, resolved, width // 2, height // 2)
    # Preserve the already visually validated two-build batch2 VRAM lifetime:
    # stage2 image conditions are constructed only AFTER stage1's transformer
    # has been released and upsampling has completed. The optimized single-
    # build path alone must pre-encode stage2 before holding transformer weights
    # so its image encoder cannot compete with those resident weights.
    stage2_images_prepared = (
        _batch_conditionings(pipeline, resolved, width, height)
        if reuse_transformer_across_stages else None
    )

    def run_stages(shared_transformer: Any | None) -> tuple[Any, Any]:
        nonlocal stage1_images, stage2_images_prepared, resolved
        seeds = (pair[0].seed, pair[1].seed)
        noiser = _IndependentGaussianNoiser(seeds, pipeline.device)
        logger.info(
            "LTX25_I2V_PAIR_STAGE_START stage=stage1 shared_transformer=%s memory=%s",
            shared_transformer is not None,
            json.dumps(_memory_pressure(), sort_keys=True),
        )
        stage1 = DISTILLED_SIGMAS.to(dtype=torch.float32, device=pipeline.device)
        stage2 = STAGE_2_DISTILLED_SIGMAS.to(dtype=torch.float32, device=pipeline.device)
        first_video, first_audio = _paired_stage(
            pipeline.stage,
            video_context=video_ctx,
            audio_context=audio_ctx,
            conditionings=stage1_images,
            noiser=noiser,
            seeds=seeds,
            sigmas=stage1,
            width=width // 2, height=height // 2,
            frames=num_frames, fps=frame_rate,
            ancestral=pipeline.use_ancestral_sampler,
            max_forward_batch_size=max_forward_batch_size,
            stage_label="stage1",
            shared_transformer=shared_transformer,
        )
        # Stage1 image conditioning is never consulted again. Release its
        # concatenated GPU latent BEFORE upsampling and loading stage2 weights.
        stage1_images = None
        _log_gpu_checkpoint("stage1_conditioning_released")
        # Upscale individually, preserving baseline's B=1 arithmetic and peak VRAM.
        logger.info(
            "LTX25_I2V_PAIR_UPSAMPLE_START shared_transformer=%s memory=%s",
            shared_transformer is not None,
            json.dumps(_memory_pressure(), sort_keys=True),
        )
        upscaled = torch.cat(
            [pipeline.upsampler(first_video.latent[i : i + 1]) for i in range(2)],
            dim=0,
        )
        # First-stage video latent is not the second-stage input: only its
        # individually upscaled result is. The audio latent remains unchanged.
        initial_audio = first_audio.latent
        del first_video, first_audio
        _log_gpu_checkpoint(
            "stage1_outputs_released_after_upsample",
            trim=full_hd_memory_cleanup,
        )
        stage2_images = stage2_images_prepared
        stage2_images_prepared = None
        if stage2_images is None:
            # A pure two-build batch2 reconstructs stage2 conditioning at
            # precisely the earlier post-upsampling point, preventing both
            # stage image latent sets from staying GPU-resident throughout.
            stage2_images = _batch_conditionings(pipeline, resolved, width, height)
        # The CRF/image references are CPU-only and no longer needed for either
        # denoising stage. A Full HD boundary trim runs only on dead GPU cache.
        resolved = None
        _log_gpu_checkpoint("stage2_conditioning_ready", trim=full_hd_memory_cleanup)
        # The stage1 GPU-resident 22B transformer is already disposed here.
        # Build the streaming stage2 wrapper only AFTER stage1 latents and the
        # image encoder have been released. Never instantiate a second live
        # transformer or reload Gemma / VAE for this mode.
        stage2 = stage2_factory() if stage2_factory is not None else pipeline.stage
        if full_hd_memory_cleanup:
            _log_gpu_checkpoint("stage2_transformer_factory_ready")
        logger.info(
            "LTX25_I2V_PAIR_STAGE_START stage=stage2 shared_transformer=%s memory=%s",
            shared_transformer is not None,
            json.dumps(_memory_pressure(), sort_keys=True),
        )
        second_video, _ = _paired_stage(
            stage2,
            video_context=video_ctx,
            audio_context=audio_ctx,
            conditionings=stage2_images,
            noiser=noiser,
            seeds=seeds,
            sigmas=stage2,
            width=width, height=height,
            frames=num_frames, fps=frame_rate,
            initial_video=upscaled,
            initial_audio=initial_audio,
            max_forward_batch_size=max_forward_batch_size,
            stage_label="stage2",
            shared_transformer=shared_transformer,
            monitor_full_hd=full_hd_memory_cleanup,
        )
        del upscaled, initial_audio, stage2_images
        _log_gpu_checkpoint("stage2_inputs_released_before_decode")
        # Decode separately: both latents are independent; never decode one clip
        # with the other's seed, or create two live VAE models.
        results = []
        for i in range(2):
            generator = noiser.generators[i]
            latent = second_video.latent[i : i + 1]
            results.append(PipelineOutput(
                _lazy_decode(pipeline, latent, tiling, generator),
                None, num_frames, tiling, None, latent,
            ))
        logger.info(
            "LTX25_I2V_PAIR_GENERATED transformer_stage_builds=%d forward_batch=%d seeds=%s",
            1 if shared_transformer is not None else 2, max_forward_batch_size, seeds,
        )
        return tuple(results)

    if reuse_transformer_across_stages:
        # This pinned FP8 single-GPU stage has no shape-dependent wrapper.
        # Hold exactly the SAME initialized weights for both denoising passes,
        # including the existing VAE upsample in between. No changes to
        # sampler, tensors or kernels. This may OOM if upsample needs the
        # retained GPU memory; caller retries the APPROVED two-build B=2 path.
        if pipeline.stage._model_wrapper is not None or pipeline.stage._is_streaming:
            raise ValueError("Stage reuse supports only unwrapped resident FP8")
        build_started = time.perf_counter()
        with pipeline.stage._transformer_ctx() as transformer:
            logger.info(
                "LTX25_I2V_PAIR_SHARED_TRANSFORMER_LOADED %s",
                json.dumps({
                    "seconds": time.perf_counter() - build_started,
                    "memory": _memory_pressure(),
                }, sort_keys=True),
            )
            return run_stages(transformer)
    return run_stages(None)
