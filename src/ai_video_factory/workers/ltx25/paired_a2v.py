"""Strict true-GPU-B2 1080p A2V; frozen per-speaker audio, pinned static recipe.

Only the video/audio transformer forwards are combined. Each reference
audio, image, prompt, seed and lazy VAE decode remains independent. This
module NEVER substitutes for the production single-video A2V pipeline.
"""

from __future__ import annotations

import gc
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import torch
from ltx_core.batch_split import BatchSplitAdapter
from ltx_core.components.diffusion_steps import (
    EulerAncestralDiffusionStep,
    EulerDiffusionStep,
)
from ltx_core.components.guiders import MultiModalGuider, MultiModalGuiderParams
from ltx_core.components.patchifiers import AudioPatchifier, VideoLatentPatchifier
from ltx_core.conditioning.types.latent_cond import VideoConditionByLatentIndex
from ltx_core.model.audio_vae import encode_audio as vae_encode_audio
from ltx_core.model.video_vae import AUTO_TILING
from ltx_core.tools import AudioLatentTools, VideoLatentTools
from ltx_core.types import Audio, AudioLatentShape, VideoLatentShape, VideoPixelShape
from ltx_pipelines.utils.blocks import _build_state
from ltx_pipelines.utils.constants import DISTILLED_SIGMAS, STAGE_2_DISTILLED_SIGMAS
from ltx_pipelines.utils.denoisers import GuidedDenoiser, SimpleDenoiser
from ltx_pipelines.utils.helpers import (
    assert_resolution,
    audio_duration_seconds,
    combined_image_conditionings,
    ensure_tiling_config,
    tiling_scale_factors_for_vae,
)
from ltx_pipelines.utils.media_io import decode_audio_from_file
from ltx_pipelines.utils.samplers import euler_ancestral_denoising_loop, euler_denoising_loop
from ltx_pipelines.utils.types import ModalitySpec, PipelineOutput

from .reference_a2v import _with_strength
from .resource_metrics import memory_pressure
from .reference_recipe import (
    LTX_A2V_REFERENCE_RECIPE,
    LTX_A2V_STATIC_IMAGE_STRENGTH,
    LTX_A2V_STATIC_NEGATIVE_PROMPT,
    LTX_A2V_STATIC_VIDEO_CFG_SCALE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class A2VSample:
    prompt: str
    seed: int
    image: Any
    audio_path: str


class IndependentA2VNoiser:
    """Separate B1-compatible RNG sequences for EACH video's frozen-audio path."""

    def __init__(self, seeds: tuple[int, int], device: Any) -> None:
        self.generators = tuple(
            torch.Generator(device=device).manual_seed(seed) for seed in seeds
        )

    def __call__(self, state: Any, noise_scale: float = 1.0) -> Any:
        # Upstream _build_state samples audio noise EVEN WHEN audio is frozen.
        # Preserve per-sample B1 draw ordering: stage1 video, stage1 audio,
        # stage2 video, stage2 audio. Each batch slice draws independently.
        from dataclasses import replace

        if state.latent.shape[0] != 2:
            raise RuntimeError("A2V RNG invariant: true batch dimension must be two")
        noise = torch.cat([
            torch.randn(
                state.latent[i:i + 1].shape, generator=g,
                device=state.latent.device, dtype=state.latent.dtype,
            )
            for i, g in enumerate(self.generators)
        ])
        latent = torch.lerp(state.latent.float(), noise.float(), noise_scale)
        latent = torch.lerp(state.clean_latent.float(), latent, state.denoise_mask)
        return replace(state, latent=latent.to(state.latent.dtype))


def _ancestral_noise(seeds: tuple[int, int], device: Any):
    generators = tuple(torch.Generator(device=device).manual_seed(
        seed + LTX_A2V_REFERENCE_RECIPE.ancestral_noise_seed_offset,
    ) for seed in seeds)

    def draw(latent: torch.Tensor, _unused: Any) -> torch.Tensor:
        if latent.shape[0] != 2:
            raise RuntimeError("Ancestral RNG must receive the real GPU batch of two")
        return torch.cat([
            torch.randn(latent[i:i + 1].shape, generator=g,
                        dtype=latent.dtype, device=latent.device)
            for i, g in enumerate(generators)
        ])
    return draw


def _batch_images(pipeline: Any, samples: tuple[A2VSample, A2VSample],
                  width: int, height: int, strength: float) -> list[Any]:
    """Perform each B1 image encode separately; stack equivalent latent tensors."""
    def encode(encoder: Any) -> list[Any]:
        results = []
        for sample in samples:
            item = combined_image_conditionings(
                images=_with_strength([sample.image], strength),
                width=width, height=height, video_encoder=encoder,
                dtype=torch.bfloat16, device=pipeline.device, color_space=None,
            )
            if len(item) != 1 or not isinstance(item[0], VideoConditionByLatentIndex):
                raise RuntimeError("A2V B2 accepts exactly one reference frame per video")
            results.append(item[0])
        return results

    first, second = pipeline.image_conditioner(encode)
    if first.strength != second.strength or first.latent_idx != second.latent_idx:
        raise RuntimeError("A2V image strengths and first-frame indices must match")
    return [VideoConditionByLatentIndex(
        latent=torch.cat([first.latent, second.latent], dim=0),
        strength=first.strength, latent_idx=first.latent_idx,
    )]


def _stage(
    stage: Any, *,
    video_ctx: torch.Tensor,
    audio_ctx: torch.Tensor,
    conditionings: list[Any],
    noiser: IndependentA2VNoiser,
    sigmas: torch.Tensor,
    width: int, height: int, frames: int, fps: float,
    initial_video: torch.Tensor | None,
    frozen_audio: torch.Tensor,
    negative_ctx: torch.Tensor | None = None,
    seeds: tuple[int, int],
    label: str,
    phase_metrics: dict[str, Any] | None = None,
) -> Any:
    """Faithful pinned DiffusionStage._build_state + frozen modality semantics."""
    
    shape = VideoPixelShape(batch=2, frames=frames, width=width, height=height, fps=fps)
    video_tools = VideoLatentTools(
        VideoLatentPatchifier(patch_size=1),
        VideoLatentShape.from_pixel_shape(shape, scale_factors=stage.video_scale_factors),
        fps, scale_factors=stage.video_scale_factors,
    )
    audio_tools = AudioLatentTools(
        AudioPatchifier(patch_size=1),
        AudioLatentShape.from_video_pixel_shape(shape),
    )
    v_spec = ModalitySpec(
        context=video_ctx, conditionings=conditionings,
        noise_scale=float(sigmas[0]),
        initial_latent=initial_video,
    )
    a_spec = ModalitySpec(
        context=audio_ctx, frozen=True, noise_scale=0.0,
        initial_latent=frozen_audio,
    )
    # Pinned upstream DiffusionStage builds the model BEFORE sampling noise.
    start = time.perf_counter()
    with stage._transformer_ctx(video_tools=video_tools) as built:
        build = time.perf_counter() - start
        transformer = built if stage._model_wrapper is None else stage._model_wrapper(
            built, video_tools
        )
        video = _build_state(v_spec, video_tools, noiser, stage._dtype, stage._device)
        audio = _build_state(a_spec, audio_tools, noiser, stage._dtype, stage._device)
        if not audio.frozen or torch.any(audio.denoise_mask):
            raise RuntimeError("A2V B2 frozen audio must never be denoised")
        if negative_ctx is None:
            denoiser = SimpleDenoiser(video_ctx, audio_ctx)
            loop = euler_denoising_loop
            stepper = EulerDiffusionStep()
            loop_options: dict[str, Any] = {}
        else:
            denoiser = GuidedDenoiser(
                v_context=video_ctx, a_context=audio_ctx,
                video_guider=MultiModalGuider(
                    params=MultiModalGuiderParams(
                        cfg_scale=LTX_A2V_STATIC_VIDEO_CFG_SCALE,
                        stg_scale=0.0, rescale_scale=0.0,
                        modality_scale=1.0, stg_blocks=[],
                    ), negative_context=negative_ctx,
                ),
                audio_guider=MultiModalGuider(params=MultiModalGuiderParams()),
            )
            loop = euler_ancestral_denoising_loop
            stepper = EulerAncestralDiffusionStep(
                eta=LTX_A2V_REFERENCE_RECIPE.ancestral_eta,
                s_noise=LTX_A2V_REFERENCE_RECIPE.ancestral_s_noise,
            )
            loop_options = {
                "noise_seed": seeds[0] + LTX_A2V_REFERENCE_RECIPE.ancestral_noise_seed_offset,
                "new_noise_fn": _ancestral_noise(seeds, stage._device),
                "model_dtype": stage._dtype,
            }
        started = time.perf_counter()
        video, audio = loop(
            sigmas=sigmas, video_state=video, audio_state=audio,
            stepper=stepper, transformer=BatchSplitAdapter(
                transformer, max_batch_size=2,
            ), denoiser=denoiser, **loop_options,
        )
        seconds = time.perf_counter() - started
        if phase_metrics is not None:
            phase_metrics[label + "_transformer_build_seconds"] = build
            phase_metrics[label + "_denoise_seconds"] = seconds
        video = video_tools.unpatchify(video_tools.clear_conditioning(video))
        audio = audio_tools.unpatchify(audio_tools.clear_conditioning(audio))
        logger.info("LTX25_A2V_B2_STAGE %s", json.dumps({
            "stage": label, "actual_gpu_batch_size": 2,
            "transformer_build_seconds": round(build, 3),
            "denoise_seconds": round(seconds, 3),
        }, sort_keys=True))
        return video, audio


def _prepare_independent_prompt_contexts(
    pipeline: Any, samples: tuple[A2VSample, A2VSample],
) -> tuple[list[tuple[A2VSample, Any, Any]], dict[str, Any]]:
    """Within this PAIR only, reuse literally identical deterministic embeddings.

    The pinned reference prompt encoder has image enhancement disabled. The
    same literal positive/negative text and identical encoder options yield
    the same embeddings. Keep two independent batch tensor slots via cat;
    never cache across jobs, seeds, images, or differently worded prompts.
    """
    prepared: list[tuple[A2VSample, Any, Any]] = []
    first_contexts: Any = None
    identical = samples[0].prompt == samples[1].prompt
    started = time.perf_counter()
    for index, sample in enumerate(samples):
        (image,) = pipeline.image_conditioner.resolve_crf([sample.image])
        if index == 1 and identical:
            contexts = first_contexts
        else:
            contexts = pipeline.prompt_encoder(
                [sample.prompt, LTX_A2V_STATIC_NEGATIVE_PROMPT],
                enhance_first_prompt=False, enhance_static_cache=False,
            )
            if len(contexts) != 2:
                raise RuntimeError(
                    "A2V static reference requires positive + negative encodes"
                )
            if index == 0:
                first_contexts = contexts
        prepared.append((
            A2VSample(sample.prompt, sample.seed, image, sample.audio_path),
            contexts[0], contexts[1],
        ))
    return prepared, {
        "identical_prompts": identical,
        "prompt_encoder_calls": 1 if identical else 2,
        "prompt_preparation_seconds": time.perf_counter() - started,
        "same_pair_context_reuse": identical,
    }


def generate_paired_static_a2v(
    pipeline: Any, samples: tuple[A2VSample, A2VSample], *,
    width: int, height: int, fps: int, frames: int,
    stage2_factory: Any,
    phase_metrics: dict[str, Any] | None = None,
) -> tuple[PipelineOutput, PipelineOutput]:
    """Generate paired A/V with independent frozen PCM and no silent B1 fallback."""
    if len(samples) != 2 or samples[0].seed == samples[1].seed:
        raise ValueError("Two independent A2V B2 seeds are mandatory")
    if (width, height, fps) != (1920, 1088, 24):
        raise ValueError("A2V B2 only supports internal 1920x1088 at 24fps")
    if (frames - 1) % 8 or frames < 9:
        raise ValueError("A2V pair must share the exact 8k+1 audio-driven frame count")
    assert_resolution(height=height, width=width, is_two_stage=True)
    if phase_metrics is not None:
        phase_metrics["memory_after_pipeline_acquire"] = memory_pressure()
    prepared, prompt_metrics = _prepare_independent_prompt_contexts(pipeline, samples)
    if phase_metrics is not None:
        phase_metrics.update(prompt_metrics)
    v_context = torch.cat([x[1].video_encoding for x in prepared], dim=0)
    a_context = torch.cat([x[1].audio_encoding for x in prepared], dim=0)
    negative = torch.cat([x[2].video_encoding for x in prepared], dim=0)
    prepared_samples = tuple(x[0] for x in prepared)

    # Two original conditioned audios are decoded and VAE-encoded separately.
    # The clip's own source PCM is later muxed into its OWN video, never
    # synthesized from the transformer's (frozen) audio prediction.
    frozen_latents = []
    outputs = []
    for sample in prepared_samples:
        decoded = decode_audio_from_file(sample.audio_path, pipeline.device, 0.0, None)
        if decoded is None:
            raise ValueError("A2V input audio decoder returned None")
        duration = frames / fps
        actual = audio_duration_seconds(decoded)
        if actual + 1.0 / decoded.sampling_rate < duration:
            raise ValueError("A2V audio does not cover the snapped paired frame duration")
        latent = pipeline.audio_conditioner(
            lambda encoder, decoded=decoded: vae_encode_audio(decoded, encoder, None)
        )
        expected = AudioLatentShape.from_duration(
            batch=1, duration=duration, channels=8, mel_bins=16,
        ).frames
        if latent.shape[2] < expected:
            raise ValueError("A2V audio encoder produced too few latent frames")
        frozen_latents.append(latent[:, :, :expected])
        outputs.append(Audio(
            waveform=decoded.waveform.squeeze(0)[..., :max(
                1, math.ceil(duration * decoded.sampling_rate),
            )],
            sampling_rate=decoded.sampling_rate,
        ))
    if frozen_latents[0].shape != frozen_latents[1].shape:
        raise ValueError("Both A2V audios must have the same frozen latent shape")
    frozen = torch.cat(frozen_latents, dim=0)
    del frozen_latents, prepared
    gc.collect()

    scale = tiling_scale_factors_for_vae(pipeline.video_decoder.checkpoint_path)
    tiling = ensure_tiling_config(
        AUTO_TILING, scale_factors=scale,
        vae_checkpoint_path=pipeline.video_decoder.checkpoint_path,
        video_shape=VideoPixelShape(1, frames, height, width, fps),
        diffvae_optimization=pipeline.video_decoder.diffvae_optimization,
        device=pipeline.device,
    )
    noiser = IndependentA2VNoiser(
        (samples[0].seed, samples[1].seed), pipeline.device,
    )
    conditioning_start = time.perf_counter()
    stage1_images = _batch_images(
        pipeline, prepared_samples, width // 2, height // 2,
        LTX_A2V_STATIC_IMAGE_STRENGTH,
    )
    if phase_metrics is not None:
        phase_metrics["stage1_image_conditioning_seconds"] = (
            time.perf_counter() - conditioning_start
        )
    logger.info("LTX25_A2V_B2_START stage=stage1 frames=%d", frames)
    stage1_video, stage1_audio = _stage(
        pipeline.stage_1, video_ctx=v_context, audio_ctx=a_context,
        conditionings=stage1_images, noiser=noiser,
        sigmas=DISTILLED_SIGMAS.to(dtype=torch.float32, device=pipeline.device),
        width=width // 2, height=height // 2, frames=frames, fps=fps,
        initial_video=None, frozen_audio=frozen,
        negative_ctx=negative, seeds=(samples[0].seed, samples[1].seed),
        label="stage1", phase_metrics=phase_metrics,
    )
    if phase_metrics is not None:
        phase_metrics["memory_after_stage1"] = memory_pressure()
    del stage1_images, stage1_audio
    upsample_start = time.perf_counter()
    upscaled = torch.cat([
        pipeline.upsampler(stage1_video.latent[i:i + 1]) for i in range(2)
    ], dim=0)
    del stage1_video
    if phase_metrics is not None:
        phase_metrics["latent_upsampling_seconds"] = time.perf_counter() - upsample_start
    conditioning_start = time.perf_counter()
    stage2_images = _batch_images(
        pipeline, prepared_samples, width, height,
        LTX_A2V_REFERENCE_RECIPE.stage_2_image_strength,
    )
    del prepared_samples
    if phase_metrics is not None:
        phase_metrics["stage2_image_conditioning_seconds"] = (
            time.perf_counter() - conditioning_start
        )
    stage2_factory_start = time.perf_counter()
    stage2 = stage2_factory()
    if phase_metrics is not None:
        phase_metrics["stage2_wrapper_factory_seconds"] = (
            time.perf_counter() - stage2_factory_start
        )
    logger.info("LTX25_A2V_B2_START stage=stage2")
    final, _ = _stage(
        stage2, video_ctx=v_context, audio_ctx=a_context,
        conditionings=stage2_images, noiser=noiser,
        sigmas=STAGE_2_DISTILLED_SIGMAS.to(
            dtype=torch.float32, device=pipeline.device,
        ),
        width=width, height=height, frames=frames, fps=fps,
        initial_video=upscaled,
        frozen_audio=frozen,
        seeds=(samples[0].seed, samples[1].seed), label="stage2",
        phase_metrics=phase_metrics,
    )
    if phase_metrics is not None:
        phase_metrics["memory_after_stage2"] = memory_pressure()
    result = []
    for i in range(2):
        latent = final.latent[i:i + 1]
        generator = noiser.generators[i]

        def lazy_decode(latent=latent, generator=generator):
            yield from pipeline.video_decoder(
                latent, tiling, generator, dtype=torch.bfloat16,
            )
        result.append(PipelineOutput(
            lazy_decode(), outputs[i], frames, tiling, None, latent,
        ))
    return tuple(result)
