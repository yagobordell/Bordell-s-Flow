"""EXPERIMENTAL two-video LTX 2.5 FP8 inference on ONE shared transformer.

A single B=2 forward is attempted per denoising step (8+3 steps). The
original per-video prompt, image conditioning, noise generators, sampler,
two-stage sigmas, FP8 quantization, VAE and output geometry are retained.
This module is *never* selected by the existing single-video task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Any

import torch
from ltx_core.batch_split import BatchSplitAdapter
from ltx_core.components.patchifiers import AudioPatchifier, VideoLatentPatchifier
from ltx_core.conditioning.types.latent_cond import VideoConditionByLatentIndex
from ltx_core.tools import AudioLatentTools, VideoLatentTools
from ltx_core.types import AudioLatentShape, VideoLatentShape, VideoPixelShape
from ltx_core.model.video_vae import AUTO_TILING
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

logger = logging.getLogger(__name__)


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
    with stage._transformer_ctx(video_tools=video_tools) as built:
        transformer = built if stage._model_wrapper is None else stage._model_wrapper(built, video_tools)
        # This is genuinely a single B=2 model forward, NOT two serialized jobs.
        batched_transformer = BatchSplitAdapter(transformer, max_batch_size=2)
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
        video_state = video_tools.unpatchify(video_tools.clear_conditioning(video_state))
        audio_state = audio_tools.unpatchify(audio_tools.clear_conditioning(audio_state))
    return video_state, audio_state


def _batch_conditionings(pipeline: Any, pair: tuple[PairInput, PairInput], width: int, height: int):
    """Encode each reference image separately to preserve the original conditioning."""
    prepared = []
    for item in pair:
        encoded = pipeline.image_conditioner(
            lambda encoder, sample=item: combined_image_conditionings(
                images=[sample.image], height=height, width=width, video_encoder=encoder,
                dtype=pipeline.dtype, device=pipeline.device,
            )
        )
        if len(encoded) != 1 or not isinstance(encoded[0], VideoConditionByLatentIndex):
            raise ValueError("Experimental batch2 supports one first-frame image per sample")
        prepared.append(encoded[0])
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
) -> tuple[Any, Any]:
    """Generate two clips in B=2 transformer forwards with B=1 quality semantics.

    Does not modify single-video production settings or its random streams.
    OOM and runtime errors are handled by the caller, never suppressed here.
    """
    from ltx_pipelines.utils.types import PipelineOutput

    if len(pair) != 2 or pair[0].seed == pair[1].seed:
        raise ValueError("Batch2 needs two independent seeds")
    assert_resolution(height=height, width=width, is_two_stage=True)
    require_num_frames_source(num_frames, pipeline.duration_predictor)
    # Preserve per-video Gemma tokenization/padding by encoding ONE prompt at a
    # time. Attempting combined text encoding could change text-conditioning.
    prepared = []
    for item in pair:
        image = pipeline.image_conditioner.resolve_crf([item.image])[0]
        (context,) = pipeline.prompt_encoder(
            [item.prompt],
            enhance_first_prompt=False,
            enhance_static_cache=False,
            enhance_prompt_image=item.image[0],
        )
        prepared.append((PairInput(item.prompt, item.seed, image), context))
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
    scale = tiling_scale_factors_for_vae(pipeline.video_decoder.checkpoint_path)
    tiling = ensure_tiling_config(
        AUTO_TILING, scale_factors=scale,
        vae_checkpoint_path=pipeline.video_decoder.checkpoint_path,
        video_shape=VideoPixelShape(1, num_frames, height, width, frame_rate),
        diffvae_optimization=pipeline.video_decoder.diffvae_optimization,
        device=pipeline.device,
    )
    seeds = (pair[0].seed, pair[1].seed)
    noiser = _IndependentGaussianNoiser(seeds, pipeline.device)
    stage1 = DISTILLED_SIGMAS.to(dtype=torch.float32, device=pipeline.device)
    stage2 = STAGE_2_DISTILLED_SIGMAS.to(dtype=torch.float32, device=pipeline.device)
    conditionings = _batch_conditionings(pipeline, resolved, width // 2, height // 2)
    first_video, first_audio = _paired_stage(
        pipeline.stage,
        video_context=video_ctx,
        audio_context=audio_ctx,
        conditionings=conditionings,
        noiser=noiser,
        seeds=seeds,
        sigmas=stage1,
        width=width // 2, height=height // 2,
        frames=num_frames, fps=frame_rate,
        ancestral=pipeline.use_ancestral_sampler,
    )
    # Upscale individually, preserving baseline's B=1 arithmetic and peak VRAM.
    upscaled = torch.cat(
        [pipeline.upsampler(first_video.latent[i : i + 1]) for i in range(2)],
        dim=0,
    )
    conditionings = _batch_conditionings(pipeline, resolved, width, height)
    second_video, _ = _paired_stage(
        pipeline.stage,
        video_context=video_ctx,
        audio_context=audio_ctx,
        conditionings=conditionings,
        noiser=noiser,
        seeds=seeds,
        sigmas=stage2,
        width=width, height=height,
        frames=num_frames, fps=frame_rate,
        initial_video=upscaled,
        initial_audio=first_audio.latent,
    )
    # Decode separately: both latents are independent; never decode one clip
    # with the other's seed, or create two live VAE models.
    results = []
    for i, item in enumerate(pair):
        generator = noiser.generators[i]
        latent = second_video.latent[i : i + 1]
        results.append(PipelineOutput(
            _lazy_decode(pipeline, latent, tiling, generator),
            None, num_frames, tiling, None, latent,
        ))
    logger.info("LTX25_I2V_BATCH2_GENERATED shared_transformer_stage_builds=2 seeds=%s", seeds)
    return tuple(results)
