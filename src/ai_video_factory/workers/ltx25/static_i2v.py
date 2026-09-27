"""Opt-in I2V camera guidance using the resident pinned distilled pipeline.

Based on LTX-2 commit a95ab856bf29407b6b066ede0abe1846050db56c
DistilledPipeline.__call__; only Stage 1 changes to camera-only negative
CFG 1.25. Stage 2, sampler, image and audio latents, decoder and seeds are
unchanged. Experimental: camera stability requires real video validation.
"""

from __future__ import annotations

from collections.abc import Sequence

import torch
from ltx_core.components.guiders import MultiModalGuider, MultiModalGuiderParams
from ltx_core.components.noisers import GaussianNoiser
from ltx_core.model.video_vae import AUTO_TILING, AutoTiling, TilingConfig
from ltx_core.types import VideoPixelShape
from ltx_pipelines.distilled import DistilledPipeline
from ltx_pipelines.utils.args import ImageConditioningInput
from ltx_pipelines.utils.blocks import require_num_frames_source, resolve_num_frames
from ltx_pipelines.utils.constants import DISTILLED_SIGMAS, STAGE_2_DISTILLED_SIGMAS
from ltx_pipelines.utils.denoisers import GuidedDenoiser, SimpleDenoiser
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

# Independently pinned recipe: changes to the A2V experiment must not silently
# alter existing I2V job identities or replay a different R2 generation.
LTX_I2V_STATIC_CAMERA_VIDEO_CFG_SCALE = 1.25
LTX_I2V_STATIC_CAMERA_NEGATIVE_PROMPT = (
    "camera zoom, zoom in, push in, dolly in, camera drift, camera shake, "
    "reframing, crop change, handheld camera, unintended camera movement"
)


def generate_static_camera(  # noqa: PLR0913
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
) -> PipelineOutput:
    """Generate a video.
    Stage 1 samples with the ancestral (SDE) Euler sampler or the deterministic one according
    to ``self.use_ancestral_sampler``, detected from the checkpoint generation. Stage 2 is
    always deterministic -- its 3-step refinement schedule is too short to remove freshly
    injected noise.
    """
    if enhance_prompt:
        raise ValueError("static-camera I2V must preserve the original positive prompt")
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

    ctx_p, ctx_n = self.prompt_encoder(
        [prompt, LTX_I2V_STATIC_CAMERA_NEGATIVE_PROMPT],
        enhance_first_prompt=enhance_prompt,
        enhance_static_cache=enhance_static_cache,
        enhance_prompt_image=images[0][0] if len(images) > 0 else None,
    )
    video_context, audio_context = ctx_p.video_encoding, ctx_p.audio_encoding
    negative_video_context = ctx_n.video_encoding

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

    video_state, audio_state = self.stage(
        denoiser=GuidedDenoiser(
            v_context=video_context,
            a_context=audio_context,
            video_guider=MultiModalGuider(
                params=MultiModalGuiderParams(
                    cfg_scale=LTX_I2V_STATIC_CAMERA_VIDEO_CFG_SCALE,
                    stg_scale=0.0,
                    rescale_scale=0.0,
                    modality_scale=1.0,
                    stg_blocks=[],
                ),
                negative_context=negative_video_context,
            ),
            audio_guider=MultiModalGuider(params=MultiModalGuiderParams()),
        ),
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
    upscaled_video_latent = self.upsampler(video_state.latent[:1])

    stage_2_sigmas = stage_2_sigmas.to(dtype=torch.float32, device=self.device)
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

    decoded_video = self.video_decoder(
        video_state.latent, tiling_config, generator, dtype=vae_dtype
    )
    decoded_audio = self.audio_decoder(audio_state.latent)
    return PipelineOutput(
        decoded_video, decoded_audio, num_frames, tiling_config, None, video_state.latent
    )

