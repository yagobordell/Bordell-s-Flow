from __future__ import annotations

from collections.abc import Sequence
from functools import partial
import math

import torch

from ltx_core.components.diffusion_steps import EulerAncestralDiffusionStep
from ltx_core.components.noisers import GaussianNoiser
from ltx_core.model.audio_vae import encode_audio as vae_encode_audio
from ltx_core.model.video_vae import AUTO_TILING, AutoTiling, TilingConfig
from ltx_core.types import Audio, AudioLatentShape, VideoPixelShape
from ltx_pipelines.a2vid_two_stage import A2VidPipelineTwoStage
from ltx_pipelines.utils.args import ImageConditioningInput
from ltx_pipelines.utils.constants import DISTILLED_SIGMAS, STAGE_2_DISTILLED_SIGMAS
from ltx_pipelines.utils.denoisers import SimpleDenoiser
from ltx_pipelines.utils.helpers import (
    assert_resolution,
    audio_duration_seconds,
    combined_image_conditionings,
    ensure_tiling_config,
    tiling_scale_factors_for_vae,
)
from ltx_pipelines.utils.media_io import HDRColorSpace, decode_audio_from_file
from ltx_pipelines.utils.samplers import euler_ancestral_denoising_loop
from ltx_pipelines.utils.types import ModalitySpec, PipelineOutput

from .reference_recipe import LTX_A2V_REFERENCE_RECIPE


def _with_strength(
    images: Sequence[ImageConditioningInput],
    strength: float,
) -> list[ImageConditioningInput]:
    return [
        ImageConditioningInput(
            path=image.path,
            frame_idx=image.frame_idx,
            strength=strength,
            crf=image.crf,
        )
        for image in images
    ]


class DistilledReferenceA2VPipeline(A2VidPipelineTwoStage):
    """Minimal A2V orchestration matching the LTX-2.5 distilled two-stage recipe.

    Model construction, encoders, diffusion stages, upsampler and decoder remain
    owned by upstream A2VidPipelineTwoStage. Only its call orchestration is
    specialized so Stage 1 uses the LTX-2.5 ancestral sampler, image strengths
    differ by stage, and padded audio is checked before frozen conditioning.
    """

    def __call__(  # noqa: PLR0913
        self,
        prompt: str,
        seed: int,
        height: int,
        width: int,
        num_frames: int,
        frame_rate: float,
        images: list[ImageConditioningInput],
        audio_path: str,
        tiling_config: TilingConfig | AutoTiling | None = AUTO_TILING,
        vae_dtype: torch.dtype | None = None,
        enhance_prompt: bool = False,
        enhance_static_cache: bool = False,
        max_batch_size: int = 1,
        stage_1_sigmas: torch.Tensor = DISTILLED_SIGMAS,
        stage_2_sigmas: torch.Tensor = STAGE_2_DISTILLED_SIGMAS,
        color_space: HDRColorSpace | None = None,
    ) -> PipelineOutput:
        if num_frames < 1 or (num_frames - 1) % 8 != 0:
            raise ValueError(
                "reference A2V num_frames must satisfy the LTX 8k+1 temporal grid"
            )
        assert_resolution(height=height, width=width, is_two_stage=True)

        images = self.image_conditioner.resolve_crf(images)
        stage_1_images = _with_strength(
            images, LTX_A2V_REFERENCE_RECIPE.stage_1_image_strength
        )
        stage_2_images = _with_strength(
            images, LTX_A2V_REFERENCE_RECIPE.stage_2_image_strength
        )

        generator = torch.Generator(device=self.device).manual_seed(seed)
        noiser = GaussianNoiser(generator=generator)
        dtype = torch.bfloat16
        if vae_dtype is None:
            vae_dtype = dtype

        decoded_audio = decode_audio_from_file(audio_path, self.device, 0.0, None)
        if decoded_audio is None:
            raise ValueError(
                f"Failed to decode audio from {audio_path}. Please check the file and try again."
            )
        video_duration = num_frames / frame_rate
        audio_duration = audio_duration_seconds(decoded_audio)
        sample_tolerance = 1.0 / float(decoded_audio.sampling_rate)
        if audio_duration + sample_tolerance < video_duration:
            raise ValueError(
                "reference A2V conditioning audio does not cover the snapped video "
                f"duration: {audio_duration:.6f}s < {video_duration:.6f}s"
            )

        (ctx_p,) = self.prompt_encoder(
            [prompt],
            enhance_first_prompt=enhance_prompt,
            enhance_static_cache=enhance_static_cache,
            enhance_prompt_image=stage_1_images[0][0] if stage_1_images else None,
        )
        v_context_p = ctx_p.video_encoding
        a_context_p = ctx_p.audio_encoding

        scale_factors = tiling_scale_factors_for_vae(self.video_decoder.checkpoint_path)
        video_shape = VideoPixelShape(
            batch=1,
            frames=num_frames,
            height=height,
            width=width,
            fps=frame_rate,
        )
        tiling_config = ensure_tiling_config(
            tiling_config,
            scale_factors=scale_factors,
            vae_checkpoint_path=self.video_decoder.checkpoint_path,
            video_shape=video_shape,
            diffvae_optimization=self.video_decoder.diffvae_optimization,
            device=self.device,
        )

        encoded_audio_latent = self.audio_conditioner(
            lambda enc: vae_encode_audio(decoded_audio, enc, None)
        )
        audio_shape = AudioLatentShape.from_duration(
            batch=1,
            duration=video_duration,
            channels=8,
            mel_bins=16,
        )
        available_audio_latent_frames = int(encoded_audio_latent.shape[2])
        if available_audio_latent_frames < audio_shape.frames:
            raise RuntimeError(
                "reference A2V Audio VAE produced too few latent frames for the "
                "snapped video duration: "
                f"{available_audio_latent_frames} < {audio_shape.frames}"
            )
        encoded_audio_latent = encoded_audio_latent[:, :, : audio_shape.frames]

        stage_1_output_shape = VideoPixelShape(
            batch=1,
            frames=num_frames,
            width=width // 2,
            height=height // 2,
            fps=frame_rate,
        )
        stage_1_conditionings = self.image_conditioner(
            lambda enc: combined_image_conditionings(
                images=stage_1_images,
                height=stage_1_output_shape.height,
                width=stage_1_output_shape.width,
                video_encoder=enc,
                dtype=dtype,
                device=self.device,
                color_space=color_space,
            )
        )

        stage_1_sampler = {
            "stepper": EulerAncestralDiffusionStep(
                eta=LTX_A2V_REFERENCE_RECIPE.ancestral_eta,
                s_noise=LTX_A2V_REFERENCE_RECIPE.ancestral_s_noise,
            ),
            "loop": partial(
                euler_ancestral_denoising_loop,
                noise_seed=seed
                + LTX_A2V_REFERENCE_RECIPE.ancestral_noise_seed_offset,
                model_dtype=self.dtype,
            ),
        }
        video_state, _ = self.stage_1(
            denoiser=SimpleDenoiser(v_context_p, a_context_p),
            sigmas=stage_1_sigmas.to(dtype=torch.float32, device=self.device),
            noiser=noiser,
            width=stage_1_output_shape.width,
            height=stage_1_output_shape.height,
            frames=num_frames,
            fps=frame_rate,
            video=ModalitySpec(
                context=v_context_p,
                conditionings=stage_1_conditionings,
            ),
            audio=ModalitySpec(
                context=a_context_p,
                frozen=LTX_A2V_REFERENCE_RECIPE.audio_frozen_stage_1,
                noise_scale=0.0,
                initial_latent=encoded_audio_latent,
            ),
            max_batch_size=max_batch_size,
            **stage_1_sampler,
        )

        upscaled_video_latent = self.upsampler(video_state.latent[:1])
        stage_2_output_shape = VideoPixelShape(
            batch=1,
            frames=num_frames,
            width=width,
            height=height,
            fps=frame_rate,
        )
        stage_2_conditionings = self.image_conditioner(
            lambda enc: combined_image_conditionings(
                images=stage_2_images,
                height=stage_2_output_shape.height,
                width=stage_2_output_shape.width,
                video_encoder=enc,
                dtype=dtype,
                device=self.device,
                color_space=color_space,
            )
        )

        video_state, _ = self.stage_2(
            denoiser=SimpleDenoiser(v_context_p, a_context_p),
            sigmas=stage_2_sigmas.to(dtype=torch.float32, device=self.device),
            noiser=noiser,
            width=width,
            height=height,
            frames=num_frames,
            fps=frame_rate,
            video=ModalitySpec(
                context=v_context_p,
                conditionings=stage_2_conditionings,
                noise_scale=stage_2_sigmas[0].item(),
                initial_latent=upscaled_video_latent,
            ),
            audio=ModalitySpec(
                context=a_context_p,
                frozen=LTX_A2V_REFERENCE_RECIPE.audio_frozen_stage_2,
                noise_scale=0.0,
                initial_latent=encoded_audio_latent,
            ),
        )

        decoded_video = self.video_decoder(
            video_state.latent,
            tiling_config,
            generator,
            dtype=vae_dtype,
        )

        # Conditioning was padded to cover the snapped grid before the
        # Audio VAE. Ceil here as well so floating-point representation cannot
        # drop the last original speech sample at mux time.
        video_samples = max(
            1,
            math.ceil(video_duration * decoded_audio.sampling_rate),
        )
        output_audio = Audio(
            waveform=decoded_audio.waveform.squeeze(0)[..., :video_samples],
            sampling_rate=decoded_audio.sampling_rate,
        )
        return PipelineOutput(
            decoded_video,
            output_audio,
            num_frames,
            tiling_config,
            None,
            video_state.latent,
        )
