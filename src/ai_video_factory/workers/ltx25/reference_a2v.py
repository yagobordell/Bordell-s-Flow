from __future__ import annotations

import math
from collections.abc import Sequence
from contextlib import nullcontext
from functools import partial

import torch
from ltx_core.components.diffusion_steps import EulerAncestralDiffusionStep
from ltx_core.components.guiders import MultiModalGuider, MultiModalGuiderParams
from ltx_core.components.noisers import GaussianNoiser
from ltx_core.model.audio_vae import encode_audio as vae_encode_audio
from ltx_core.model.video_vae import AUTO_TILING, AutoTiling, TilingConfig
from ltx_core.types import Audio, AudioLatentShape, VideoPixelShape
from ltx_pipelines.a2vid_two_stage import A2VidPipelineTwoStage
from ltx_pipelines.utils.args import ImageConditioningInput
from ltx_pipelines.utils.constants import DISTILLED_SIGMAS, STAGE_2_DISTILLED_SIGMAS
from ltx_pipelines.utils.denoisers import GuidedDenoiser, SimpleDenoiser
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

from ai_video_factory.inference.gpu_diagnostics import diagnostic_checkpoint

from .a2v_prompt_cache import A2VBenchmarkPromptCache
from .a2v_prompt_metrics import profile_a2v_prompt_encoder
from .i2v_metrics import I2VPhaseMetrics
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

    def configure_experimental_streaming_guard(self) -> None:
        from .a2v_streaming_guard import configure_experimental_a2v_streaming

        configure_experimental_a2v_streaming(self)

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
        negative_prompt: str | None = None,
        stage_1_image_strength: float | None = None,
        video_cfg_scale: float = 1.0,
        metrics: I2VPhaseMetrics | None = None,
    ) -> PipelineOutput:
        phase = metrics.phase if metrics is not None else lambda _: nullcontext()
        if num_frames < 1 or (num_frames - 1) % 8 != 0:
            raise ValueError(
                "reference A2V num_frames must satisfy the LTX 8k+1 temporal grid"
            )
        assert_resolution(height=height, width=width, is_two_stage=True)
        if negative_prompt is not None and (not negative_prompt.strip() or video_cfg_scale <= 1.0):
            raise ValueError("negative prompt requires nonempty text and active CFG > 1")
        if negative_prompt is None and video_cfg_scale != 1.0:
            raise ValueError("reference CFG must stay 1 without explicit negative guidance")
        if negative_prompt is not None and enhance_prompt:
            raise ValueError("static A2V must preserve the exact positive prompt")
        strength = (
            LTX_A2V_REFERENCE_RECIPE.stage_1_image_strength
            if stage_1_image_strength is None
            else stage_1_image_strength
        )
        if not 0.0 < strength <= 1.0:
            raise ValueError("Stage 1 image conditioning strength must be in (0, 1]")

        diagnostic_checkpoint("reference.image.resolve.start")
        images = self.image_conditioner.resolve_crf(images)
        stage_1_images = _with_strength(
            images, strength
        )
        stage_2_images = _with_strength(
            images, LTX_A2V_REFERENCE_RECIPE.stage_2_image_strength
        )

        generator = torch.Generator(device=self.device).manual_seed(seed)
        noiser = GaussianNoiser(generator=generator)
        dtype = torch.bfloat16
        if vae_dtype is None:
            vae_dtype = dtype

        diagnostic_checkpoint("reference.audio.decode.start")
        with phase("audio_input_decode_seconds"):
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

        # The original reference does exactly one positive encode. Only the
        # opt-in static variant encodes a *separate* negative; positive text is
        # passed unchanged and the prompt enhancer is explicitly disabled.
        diagnostic_checkpoint("reference.gemma.prompt_encode.start")
        prompts = (prompt,) if negative_prompt is None else (prompt, negative_prompt)
        # Cache only post-connector tensors and only for controlled, unchanged
        # experimental prompts. The original production path is untouched.
        use_cache = metrics is not None and not enhance_prompt and not enhance_static_cache
        cache = None
        if use_cache:
            cache = getattr(self, "_benchmark_prompt_cache", None)
            if cache is None:
                cache = A2VBenchmarkPromptCache()
                self._benchmark_prompt_cache = cache
        with phase("prompt_encode_seconds"):
            contexts = None
            if cache is not None:
                with phase("prompt_cache_restore_seconds"):
                    contexts = cache.restore(prompts, device=self.device)
            if metrics is not None:
                metrics.timings["prompt_cache_hit"] = float(contexts is not None)
                # The smoke contract expects every instrumented phase; an
                # intentional cache hit has zero Gemma load/release/build.
                if contexts is not None:
                    for name in (
                        "prompt_model_acquire_seconds",
                        "prompt_model_release_seconds",
                        "prompt_embeddings_processor_build_seconds",
                    ):
                        metrics.timings[name] = 0.0
            if contexts is None:
                prompt_profiler = (
                    profile_a2v_prompt_encoder(self.prompt_encoder, metrics)
                    if metrics is not None else nullcontext()
                )
                with prompt_profiler:
                    contexts = self.prompt_encoder(
                        list(prompts),
                        enhance_first_prompt=enhance_prompt,
                        enhance_static_cache=enhance_static_cache,
                        enhance_prompt_image=stage_1_images[0][0] if stage_1_images else None,
                    )
                if cache is not None:
                    with phase("prompt_cache_store_seconds"):
                        cache.store(prompts, contexts)
        if metrics is not None:
            metrics.timings["prompt_other_execution_seconds"] = max(
                0.0,
                metrics.timings["prompt_encode_seconds"]
                - metrics.timings.get("prompt_model_acquire_seconds", 0.0)
                - metrics.timings.get("prompt_model_release_seconds", 0.0)
                - metrics.timings.get("prompt_embeddings_processor_build_seconds", 0.0),
            )
        diagnostic_checkpoint("reference.gemma.prompt_encode.done", sync_cuda=True)
        ctx_p = contexts[0]
        v_context_p = ctx_p.video_encoding
        a_context_p = ctx_p.audio_encoding
        if negative_prompt is None:
            stage_1_denoiser = SimpleDenoiser(v_context_p, a_context_p)
        else:
            stage_1_denoiser = GuidedDenoiser(
                v_context=v_context_p,
                a_context=a_context_p,
                video_guider=MultiModalGuider(
                    params=MultiModalGuiderParams(
                        cfg_scale=video_cfg_scale,
                        stg_scale=0.0,
                        rescale_scale=0.0,
                        modality_scale=1.0,
                        stg_blocks=[],
                    ),
                    negative_context=contexts[1].video_encoding,
                ),
                audio_guider=MultiModalGuider(params=MultiModalGuiderParams()),
            )

        scale_factors = tiling_scale_factors_for_vae(self.video_decoder.checkpoint_path)
        video_shape = VideoPixelShape(
            batch=1,
            frames=num_frames,
            height=height,
            width=width,
            fps=frame_rate,
        )
        diagnostic_checkpoint("reference.diffvae.tiling_plan.start")
        tiling_config = ensure_tiling_config(
            tiling_config,
            scale_factors=scale_factors,
            vae_checkpoint_path=self.video_decoder.checkpoint_path,
            video_shape=video_shape,
            diffvae_optimization=self.video_decoder.diffvae_optimization,
            device=self.device,
        )

        diagnostic_checkpoint("reference.audio_vae.encode.start")
        with phase("audio_vae_encode_seconds"):
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
        diagnostic_checkpoint("reference.image_conditioning.stage1.start")
        with phase("image_conditioning_stage1_seconds"):
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
        diagnostic_checkpoint("reference.diffusion.stage1.start")
        with phase("stage1_diffusion_seconds"):
            video_state, _ = self.stage_1(
                denoiser=stage_1_denoiser,
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

        diagnostic_checkpoint("reference.diffusion.stage1.done", sync_cuda=True)
        diagnostic_checkpoint("reference.latent.upsample.start")
        with phase("spatial_upsample_seconds"):
            upscaled_video_latent = self.upsampler(video_state.latent[:1])
        stage_2_output_shape = VideoPixelShape(
            batch=1,
            frames=num_frames,
            width=width,
            height=height,
            fps=frame_rate,
        )
        diagnostic_checkpoint("reference.image_conditioning.stage2.start")
        with phase("image_conditioning_stage2_seconds"):
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

        diagnostic_checkpoint("reference.diffusion.stage2.start")
        with phase("stage2_diffusion_seconds"):
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

        diagnostic_checkpoint("reference.diffusion.stage2.done", sync_cuda=True)
        diagnostic_checkpoint("reference.diffvae.decode.start")
        # Video decoder returns a lazy iterator; actual kernel time is timed
        # in the worker as encode_video consumes it, not in this call.
        decoded_video = self.video_decoder(
            video_state.latent,
            tiling_config,
            generator,
            dtype=vae_dtype,
        )

        diagnostic_checkpoint("reference.diffvae.decode.done", sync_cuda=True)

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
