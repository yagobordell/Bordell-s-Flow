"""Instrumented LTX 2.5 distilled vanilla I2V on upstream commit a95ab856.

Stage 1/2, sampler and joint video/audio latents mirror pinned upstream
DistilledPipeline.__call__. No camera CFG is applied. The final unused audio
waveform decode can be omitted without changing either diffusion stage.
This experimental implementation is isolated from production guided I2V.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext

import torch
from ltx_core.components.noisers import GaussianNoiser
from ltx_core.model.video_vae import AUTO_TILING, AutoTiling, TilingConfig
from ltx_core.types import VideoPixelShape
from ltx_pipelines.distilled import DistilledPipeline
from ltx_pipelines.utils.args import ImageConditioningInput
from ltx_pipelines.utils.blocks import require_num_frames_source, resolve_num_frames
from ltx_pipelines.utils.constants import DISTILLED_SIGMAS, STAGE_2_DISTILLED_SIGMAS
from ltx_pipelines.utils.denoisers import SimpleDenoiser
from ltx_pipelines.utils.helpers import (
    assert_resolution,
    combined_image_conditionings,
    ensure_tiling_config,
    generated_keyframe_conditionings,
    has_generated_keyframes,
    tiling_scale_factors_for_vae,
)
from ltx_pipelines.utils.media_io import HDRColorSpace
from ltx_pipelines.utils.types import (
    DEFAULT_AUTO_DURATION,
    AutoDuration,
    ModalitySpec,
    PipelineOutput,
)

from .i2v_metrics import I2VPhaseMetrics

def generate_fast_distilled(  # noqa: PLR0913
    self: DistilledPipeline,
    prompt: str,
    seed: int,
    height: int,
    width: int,
    frame_rate: float,
    images: list[ImageConditioningInput],
    num_frames: int | AutoDuration = DEFAULT_AUTO_DURATION,
    vae_dtype: torch.dtype | None = None,
    tiling_config: TilingConfig | AutoTiling | None = AUTO_TILING,
    enhance_prompt: bool = False,
    enhance_static_cache: bool = False,
    stage_1_sigmas: torch.Tensor = DISTILLED_SIGMAS,
    stage_2_sigmas: torch.Tensor = STAGE_2_DISTILLED_SIGMAS,
    color_space: HDRColorSpace | None = None,
    generated_keyframes: int | Sequence[int] = 0,
    decode_audio: bool = False,
    metrics: I2VPhaseMetrics | None = None,
) -> PipelineOutput:
    """Generate using pinned upstream's vanilla stage-1 and stage-2 SimpleDenoiser.
    Stage 1 samples with the ancestral (SDE) Euler sampler or the deterministic one according
    to ``self.use_ancestral_sampler``, detected from the checkpoint generation. Stage 2 is
    always deterministic -- its 3-step refinement schedule is too short to remove freshly
    injected noise.
    """
    require_num_frames_source(num_frames, self.duration_predictor)
    images = self.image_conditioner.resolve_crf(images)
    assert_resolution(height=height, width=width, is_two_stage=True)
    if has_generated_keyframes(generated_keyframes):
        self.stage.assert_generated_keyframes_supported()

    generator = torch.Generator(device=self.device).manual_seed(seed)
    noiser = GaussianNoiser(generator=generator)
    dtype = torch.bfloat16
    if vae_dtype is None:
        vae_dtype = dtype

    prompt_phase = metrics.phase("prompt_encode_seconds") if metrics else nullcontext()
    with prompt_phase:
        (ctx_p,) = self.prompt_encoder(
            [prompt],
            enhance_first_prompt=enhance_prompt,
            enhance_static_cache=enhance_static_cache,
            enhance_prompt_image=images[0][0] if len(images) > 0 else None,
        )
    video_context, audio_context = ctx_p.video_encoding, ctx_p.audio_encoding

    num_frames = resolve_num_frames(
        num_frames,
        self.duration_predictor,
        video_encoding=video_context,
        audio_encoding=audio_context,
        frame_rate=frame_rate,
    )

    scale_factors = tiling_scale_factors_for_vae(self.video_decoder.checkpoint_path)
    tiling_config = ensure_tiling_config(
        tiling_config,
        scale_factors=scale_factors,
        vae_checkpoint_path=self.video_decoder.checkpoint_path,
        video_shape=VideoPixelShape(
            batch=1, frames=num_frames, height=height, width=width, fps=frame_rate
        ),
        diffvae_optimization=self.video_decoder.diffvae_optimization,
        device=self.device,
    )

    # Stage 1: Initial low resolution video generation.
    stage_1_sigmas = stage_1_sigmas.to(dtype=torch.float32, device=self.device)
    stage_1_w, stage_1_h = width // 2, height // 2
    conditioning_phase = (
        metrics.phase("image_conditioning_stage1_seconds") if metrics else nullcontext()
    )
    with conditioning_phase:
        stage_1_conditionings = self.image_conditioner(
            lambda enc: combined_image_conditionings(
                images=images,
                height=stage_1_h,
                width=stage_1_w,
                video_encoder=enc,
                dtype=dtype,
                device=self.device,
                color_space=color_space,
            )
        )
    stage_1_conditionings.extend(generated_keyframe_conditionings(generated_keyframes, num_frames))

    stage_1_phase = metrics.phase("stage1_total_seconds") if metrics else nullcontext()
    with stage_1_phase:
        video_state, audio_state = self.stage(
            denoiser=SimpleDenoiser(video_context, audio_context),
        sigmas=stage_1_sigmas,
        noiser=noiser,
        width=stage_1_w,
        height=stage_1_h,
        frames=num_frames,
        fps=frame_rate,
        video=ModalitySpec(context=video_context, conditionings=stage_1_conditionings),
        audio=ModalitySpec(context=audio_context),
            **self._stage_1_sampler_kwargs(seed),
        )

    # Stage 2: Upsample and refine the video at higher resolution with distilled LORA.
    upsample_phase = metrics.phase("spatial_upsample_seconds") if metrics else nullcontext()
    with upsample_phase:
        upscaled_video_latent = self.upsampler(video_state.latent[:1])

    stage_2_sigmas = stage_2_sigmas.to(dtype=torch.float32, device=self.device)
    conditioning_phase = (
        metrics.phase("image_conditioning_stage2_seconds") if metrics else nullcontext()
    )
    with conditioning_phase:
        stage_2_conditionings = self.image_conditioner(
            lambda enc: combined_image_conditionings(
                images=images,
                height=height,
                width=width,
                video_encoder=enc,
                dtype=dtype,
                device=self.device,
                color_space=color_space,
            )
        )

    stage_2_phase = metrics.phase("stage2_total_seconds") if metrics else nullcontext()
    with stage_2_phase:
        video_state, audio_state = self.stage(
            denoiser=SimpleDenoiser(video_context, audio_context),
        sigmas=stage_2_sigmas,
        noiser=noiser,
        width=width,
        height=height,
        frames=num_frames,
        fps=frame_rate,
        video=ModalitySpec(
            context=video_context,
            conditionings=stage_2_conditionings,
            noise_scale=stage_2_sigmas[0].item(),
            initial_latent=upscaled_video_latent,
        ),
        audio=ModalitySpec(
            context=audio_context,
            noise_scale=stage_2_sigmas[0].item(),
            initial_latent=audio_state.latent,
        ),
        )

    # VideoDecoder returns a lazy iterator. The worker times chunk consumption
    # during encode, where GPU decoding actually happens.
    decoded_video = self.video_decoder(
        video_state.latent, tiling_config, generator, dtype=vae_dtype
    )
    decoded_audio = None
    if decode_audio:
        audio_phase = metrics.phase("audio_decode_seconds") if metrics else nullcontext()
        with audio_phase:
            decoded_audio = self.audio_decoder(audio_state.latent)
    elif metrics is not None:
        metrics.timings["audio_decode_seconds"] = 0.0
    return PipelineOutput(
        decoded_video, decoded_audio, num_frames, tiling_config, None, video_state.latent
    )

