from __future__ import annotations

import gc
import json
import logging
import math
import threading
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_video_factory.image_contracts import (
    QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    QWEN_IMAGE_21_PRODUCTION_WIDTH,
)
from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.gpu_failures import is_retryable_gpu_failure
from ai_video_factory.inference.ports import LocalArtifact

from .bootstrap import require_runtime_bootstrap_complete
from .i2v_metrics import I2VPhaseMetrics

logger = logging.getLogger(__name__)

LTX_VIDEO_TASK = "video.ltx25.generate"
LTX_GENERATION_PROFILE = "ltx25-distilled-a95ab856-fp8cpu-gridpad-eagersdpa-directcamneg-v5"
LTX_STATIC_CAMERA_GENERATION_PROFILE = (
    "ltx25-distilled-static-camera-a95ab856-fp8cpu-gridpad-eagersdpa-v1"
)
# Each material recipe change has a distinct job identity and R2 output key.
LTX_CFG_BATCH2_PROFILE = "ltx25-directcamneg-cfgb2-a95ab856-fp8cpu-eager-v1"
LTX_CFG_BATCH2_SILENT_PROFILE = "ltx25-directcamneg-cfgb2-silent-a95ab856-fp8cpu-eager-v1"
LTX_CFG_BATCH2_TRITON_PROFILE = "ltx25-directcamneg-cfgb2-silent-a95ab856-fp8cpu-triton-v1"
# Fast recipes never replace the default guided identity without a full
# paid RTX 5090 5-second performance + visual-quality promotion gate.
LTX_FAST_VANILLA_REFERENCE_PROFILE = "ltx25-fast-vanilla-upstream-native-fp8cpu-a95ab-v1"
LTX_FAST_VANILLA_PROFILE = "ltx25-fast-vanilla-silent-native-fp8cpu-a95ab-v1"
LTX_FAST_LOWRES_PROFILE = "ltx25-fast-vanilla-silent-1024x576-fp8cpu-eager-a95ab-v1"
LTX_FAST_LOWRES_TRITON_PROFILE = "ltx25-fast-vanilla-silent-1024x576-fp8cpu-triton-a95ab-v1"
LTX_FAST_LOWRES_NO_OFFLOAD_PROFILE = "ltx25-fast-vanilla-silent-1024x576-fp8none-eager-a95ab-v1"
LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE = "ltx25-fast-vanilla-silent-native-fp8none-eager-a95ab-v1"
# Production I2V uses the RTX 5090 full-resolution FP8 no-offload profile.
# Preserve LTX_GENERATION_PROFILE for legacy guided jobs and v4 clip reuse.
LTX_PRODUCTION_I2V_PROFILE = LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE
LTX_FAST_FULLRES_TRITON_PROFILE = "ltx25-fast-vanilla-silent-native-fp8cpu-triton-a95ab-v1"
LTX_FAST_FULLRES_NVFP4_PROFILE = "ltx25-fast-vanilla-silent-native-nvfp4none-eager-a95ab-v1"
_NVFP4_TRANSFORMER = "diffusion_models/ltx-2.5-22b-distilled-transformer-nvfp4.safetensors"
LTX_FULLRES_NO_OFFLOAD_PROFILES = (
    LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE,
    LTX_FAST_FULLRES_NVFP4_PROFILE,
)
LTX_FAST_PROFILES = (
    LTX_FAST_VANILLA_REFERENCE_PROFILE,
    LTX_FAST_VANILLA_PROFILE,
    LTX_FAST_LOWRES_PROFILE,
    LTX_FAST_LOWRES_TRITON_PROFILE,
    LTX_FAST_LOWRES_NO_OFFLOAD_PROFILE,
    LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE,
    LTX_FAST_FULLRES_TRITON_PROFILE,
    LTX_FAST_FULLRES_NVFP4_PROFILE,
)
LTX_FAST_LOWRES_PROFILES = (
    LTX_FAST_LOWRES_PROFILE,
    LTX_FAST_LOWRES_TRITON_PROFILE,
    LTX_FAST_LOWRES_NO_OFFLOAD_PROFILE,
)
# Forced-fresh quality check: identical default visual recipe and seed, but
# distinct Postgres/R2 identity prevents replay of the historical cached MP4.
LTX_DEFAULT_SILENT_VERIFY_PROFILE = (
    "ltx25-distilled-a95ab856-fp8cpu-gridpad-eagersdpa-directcamneg-silent-verify-v1"
)
LTX_EXPERIMENTAL_PROFILES = (
    LTX_CFG_BATCH2_PROFILE,
    LTX_CFG_BATCH2_SILENT_PROFILE,
    LTX_CFG_BATCH2_TRITON_PROFILE,
)
LTX_SUPPORTED_I2V_PROFILES = (
    LTX_GENERATION_PROFILE,
    LTX_STATIC_CAMERA_GENERATION_PROFILE,
    LTX_DEFAULT_SILENT_VERIFY_PROFILE,
    *LTX_EXPERIMENTAL_PROFILES,
    *LTX_FAST_PROFILES,
)
_CANONICAL_LANDSCAPE_SIZE = (1280, 720)
_FULL_HD_LANDSCAPE_SIZE = (1920, 1080)
_LTX_TWO_STAGE_SPATIAL_GRID = 64

_TRANSFORMER = "diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors"
_TEXT_ENCODER = "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors"
_VIDEO_VAE = "vae/ltx-2.5-video-vae-bf16.safetensors"
_AUDIO_VAE = "vae/ltx-2.5-audio-vae-bf16.safetensors"
_SPATIAL_UPSAMPLER = (
    "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"
)


class LTXVideoParameters(BaseModel):
    """Validated LTX-2.5 parameters carried inside an inference job request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    prompt: str = Field(min_length=1, max_length=8000)
    seed: int = Field(ge=0, le=2**63 - 1)
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
    fps: int = Field(gt=0, le=120)
    num_frames: int = Field(gt=0, le=10000)

    @field_validator("generation_profile")
    @classmethod
    def validate_generation_profile(cls, value: str) -> str:
        if value not in LTX_SUPPORTED_I2V_PROFILES:
            raise ValueError(
                "generation_profile must be a supported I2V profile"
            )
        return value

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must contain non-whitespace text")
        return normalized

    @model_validator(mode="after")
    def validate_ltx_shape(self) -> LTXVideoParameters:
        on_native_grid = (
            self.width % _LTX_TWO_STAGE_SPATIAL_GRID == 0
            and self.height % _LTX_TWO_STAGE_SPATIAL_GRID == 0
        )
        if not on_native_grid and (self.width, self.height) not in (
            _CANONICAL_LANDSCAPE_SIZE, _FULL_HD_LANDSCAPE_SIZE,
        ):
            raise ValueError(
                "LTX-2.5 two-stage dimensions must be divisible by 64, except the "
                "approved 1280x720 and 1920x1080 delivery, which use deterministic "
                "internal grid padding"
            )
        if (self.num_frames - 1) % 8 != 0:
            raise ValueError("LTX-2.5 num_frames must satisfy 8k + 1")
        return self


def _round_up_to_grid(value: int, grid: int = _LTX_TWO_STAGE_SPATIAL_GRID) -> int:
    return ((value + grid - 1) // grid) * grid


def _pipeline_dimensions(parameters: LTXVideoParameters) -> tuple[int, int]:
    """Return model-grid dimensions while keeping the public artifact contract unchanged."""

    return (
        _round_up_to_grid(parameters.width),
        _round_up_to_grid(parameters.height),
    )


def _fit_image_with_edge_padding(
    image: Image.Image, *, width: int, height: int
) -> Image.Image:
    """Fit the whole image without cropping or stretching, extending edge pixels."""

    scale = min(width / image.width, height / image.height)
    fitted_width = min(width, max(1, round(image.width * scale)))
    fitted_height = min(height, max(1, round(image.height * scale)))
    fitted = image.resize((fitted_width, fitted_height), Image.Resampling.LANCZOS)
    left = (width - fitted_width) // 2
    top = (height - fitted_height) // 2
    right = width - fitted_width - left
    bottom = height - fitted_height - top
    canvas = Image.new("RGB", (width, height))
    canvas.paste(fitted, (left, top))
    if left:
        canvas.paste(fitted.crop((0, 0, 1, fitted_height)).resize((left, fitted_height)), (0, top))
    if right:
        canvas.paste(
            fitted.crop((fitted_width - 1, 0, fitted_width, fitted_height)).resize(
                (right, fitted_height)
            ),
            (left + fitted_width, top),
        )
    if top:
        canvas.paste(canvas.crop((0, top, width, top + 1)).resize((width, top)), (0, 0))
    if bottom:
        canvas.paste(
            canvas.crop((0, top + fitted_height - 1, width, top + fitted_height)).resize(
                (width, bottom)
            ),
            (0, top + fitted_height),
        )
    return canvas


def _prepare_grid_keyframe(
    source: Path,
    destination: Path,
    *,
    requested_width: int,
    requested_height: int,
    pipeline_width: int,
    pipeline_height: int,
) -> Path:
    """Preserve the full keyframe before extending only the LTX model-grid margins."""

    with Image.open(source) as opened:
        image = opened.convert("RGB")
    is_qwen_keyframe = (image.width, image.height) == (
        QWEN_IMAGE_21_PRODUCTION_WIDTH,
        QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    )
    if image.width * requested_height != image.height * requested_width and not is_qwen_keyframe:
        raise ValueError(
            "LTX keyframe must match the requested video aspect ratio or use the "
            f"Qwen 1280x736 size; found {image.width}x{image.height}"
        )

    resized = _fit_image_with_edge_padding(
        image, width=requested_width, height=requested_height
    )
    if (pipeline_width, pipeline_height) == (requested_width, requested_height):
        destination.parent.mkdir(parents=True, exist_ok=True)
        resized.save(destination, format="PNG")
        return destination

    if pipeline_width != requested_width:
        raise ValueError("Canonical LTX grid adapter only supports vertical model-grid padding")
    if pipeline_height < requested_height:
        raise ValueError("LTX model-grid height cannot be smaller than requested height")

    pad_total = pipeline_height - requested_height
    pad_top = pad_total // 2
    pad_bottom = pad_total - pad_top
    canvas = Image.new("RGB", (pipeline_width, pipeline_height))
    canvas.paste(resized, (0, pad_top))
    if pad_top:
        top_row = resized.crop((0, 0, requested_width, 1)).resize(
            (requested_width, pad_top)
        )
        canvas.paste(top_row, (0, 0))
    if pad_bottom:
        bottom_row = resized.crop(
            (0, requested_height - 1, requested_width, requested_height)
        ).resize((requested_width, pad_bottom))
        canvas.paste(bottom_row, (0, pad_top + requested_height))

    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, format="PNG")
    return destination


def _crop_video_chunk_to_requested(
    chunk: Any,
    *,
    requested_width: int,
    requested_height: int,
) -> Any:
    """Center-crop one upstream decoded [F, H, W, C] video chunk."""

    shape = getattr(chunk, "shape", None)
    if shape is None or len(shape) < 4:
        raise RuntimeError(
            "LTX decoded video chunk does not expose [F, H, W, C] dimensions"
        )
    source_height = int(shape[-3])
    source_width = int(shape[-2])
    if source_width < requested_width or source_height < requested_height:
        raise RuntimeError(
            "LTX decoded video chunk is smaller than the requested output dimensions"
        )
    left = (source_width - requested_width) // 2
    top = (source_height - requested_height) // 2
    return chunk[
        ...,
        top : top + requested_height,
        left : left + requested_width,
        :,
    ]


def _crop_video_to_requested(
    video: Any,
    *,
    requested_width: int,
    requested_height: int,
) -> Any:
    """Stream-center-crop upstream LTX [F, H, W, C] decoded chunks."""

    def cropped_chunks():
        for chunk in video:
            yield _crop_video_chunk_to_requested(
                chunk,
                requested_width=requested_width,
                requested_height=requested_height,
            )

    return cropped_chunks()


def ltx_num_frames_for_duration(duration_seconds: float, *, fps: int = 24) -> int:
    """Round a target duration up to the next LTX-valid ``8k + 1`` frame count."""

    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration_seconds must be a positive finite number")
    if fps <= 0:
        raise ValueError("fps must be positive")

    minimum_frames = max(1, math.ceil(duration_seconds * fps))
    temporal_steps = (minimum_frames - 1 + 7) // 8
    return 1 + temporal_steps * 8


@dataclass(frozen=True, slots=True)
class LTXModelFiles:
    transformer: Path
    text_encoder: Path
    video_vae: Path
    audio_vae: Path
    spatial_upsampler: Path

    @classmethod
    def from_root(cls, root: Path) -> LTXModelFiles:
        return cls(
            transformer=root / _TRANSFORMER,
            text_encoder=root / _TEXT_ENCODER,
            video_vae=root / _VIDEO_VAE,
            audio_vae=root / _AUDIO_VAE,
            spatial_upsampler=root / _SPATIAL_UPSAMPLER,
        )

    def validate(self) -> None:
        missing = [str(path) for path in self.paths() if not path.is_file()]
        if missing:
            raise FileNotFoundError("Missing LTX-2.5 model files: " + ", ".join(missing))

    def paths(self) -> tuple[Path, ...]:
        return (
            self.transformer,
            self.text_encoder,
            self.video_vae,
            self.audio_vae,
            self.spatial_upsampler,
        )


class LTXVideoBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def generate(
        self,
        *,
        keyframe_path: Path,
        output_path: Path,
        parameters: LTXVideoParameters,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _LTXBindings:
    torch: Any
    distilled_pipeline: Any
    model_paths: Any
    quantization_kind: Any
    offload_mode: Any
    image_conditioning_input: Any
    encode_video: Any
    get_video_chunks_number: Any
    diffvae_apply: Any
    triton_probe: Any = None


def _load_ltx_bindings() -> _LTXBindings:
    try:
        import torch
        from ltx_core.model.video_vae import get_video_chunks_number
        from ltx_core.model.video_vae.transformer import apply as diffvae_apply
        from ltx_core.model.video_vae.transformer.fallback_na import triton_na_available
        from ltx_pipelines.distilled import DistilledPipeline
        from ltx_pipelines.utils.args import ImageConditioningInput
        from ltx_pipelines.utils.media_io import encode_video
        from ltx_pipelines.utils.model_paths import ModelPaths
        from ltx_pipelines.utils.quantization_factory import QuantizationKind
        from ltx_pipelines.utils.types import OffloadMode
    except ImportError as exc:
        raise RuntimeError(
            "LTX-2.5 runtime dependencies are not installed in this environment"
        ) from exc

    return _LTXBindings(
        torch=torch,
        distilled_pipeline=DistilledPipeline,
        model_paths=ModelPaths,
        quantization_kind=QuantizationKind,
        offload_mode=OffloadMode,
        image_conditioning_input=ImageConditioningInput,
        encode_video=encode_video,
        get_video_chunks_number=get_video_chunks_number,
        diffvae_apply=diffvae_apply,
        triton_probe=triton_na_available,
    )


def _load_static_camera_generator() -> Any:
    # Import upstream pipeline modules only on the GPU worker, never in local CLI.
    from .static_i2v import generate_static_camera

    return generate_static_camera


def _load_default_i2v_negative_guidance() -> tuple[Any, str]:
    # The production I2V profile uses the same resident weights and sampler recipe,
    # adding only fixed negative CFG guidance against direct-to-camera speech.
    from .static_i2v import (
        LTX_I2V_DIRECT_TO_CAMERA_NEGATIVE_PROMPT,
        generate_static_camera,
    )

    return generate_static_camera, LTX_I2V_DIRECT_TO_CAMERA_NEGATIVE_PROMPT


def _torch_inference_context(torch_module: Any) -> Any:
    inference_mode = getattr(torch_module, "inference_mode", None)
    if inference_mode is None:
        return nullcontext()
    return inference_mode()


def _force_diffvae_eager_sdpa(diffvae_apply: Any) -> None:
    """Force LTX DiffVAE onto its upstream PyTorch eager-SDPA fallback.

    The RTX 5090 Salad worker hit a native segmentation fault inside the default
    NATTEN-backed decode path for the canonical landscape grid.  LTX upstream
    already supports eager tiled SDPA as a compatibility backend; making both
    native fallback probes report unavailable routes CHUNKED_EAGER through that
    implementation without changing checkpoints, denoising schedules, or seeds.
    """

    diffvae_apply.natten_available = lambda: False
    diffvae_apply.triton_na_available = lambda: False


def _select_diffvae_backend(bindings: _LTXBindings, requested: str) -> str:
    """Never enable crash-prone NATTEN on the RTX 5090 pinned runtime."""
    _force_diffvae_eager_sdpa(bindings.diffvae_apply)
    if requested == "triton":
        if callable(bindings.triton_probe) and bindings.triton_probe():
            bindings.diffvae_apply.triton_na_available = bindings.triton_probe
            return "triton"
        logger.warning("LTX25_DIFFVAE_TRITON_UNAVAILABLE using=eager")
    return "eager"


class LTXPipelineModeController:
    """Serialize LTX modes and keep at most one 22B transformer pipeline resident."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._active_mode: str | None = None
        self._releasers: dict[str, Any] = {}

    @property
    def active_mode(self) -> str | None:
        return self._active_mode

    def register(self, mode: str, release: Any) -> None:
        with self.lock:
            self._releasers[mode] = release

    def activate(self, mode: str) -> None:
        with self.lock:
            if self._active_mode == mode:
                return
            previous = self._active_mode
            if previous is not None:
                release = self._releasers.get(previous)
                if release is not None:
                    release()
            self._active_mode = mode


class DirectLTX25Backend:
    """Resident, serialized direct-Python adapter around the validated LTX-2.5 pipeline."""

    def __init__(
        self,
        *,
        model_root: Path,
        device: str = "cuda",
        mode_controller: LTXPipelineModeController | None = None,
    ) -> None:
        self._model_files = LTXModelFiles.from_root(model_root)
        self._device = device
        self._pipeline: Any | None = None
        self._bindings: _LTXBindings | None = None
        self._prepared = False
        self._pipeline_requested_diffvae_backend: str | None = None
        self._pipeline_effective_diffvae_backend: str | None = None
        self._pipeline_requested_offload_mode: str = "cpu"
        self._pipeline_requested_quantization: str = "fp8_cast"
        # A prepared pipeline object can still defer heavy weight loading until its first run.
        self._pipeline_successful_generations = 0
        self._mode_controller = mode_controller or LTXPipelineModeController()
        self._lock = self._mode_controller.lock
        self._mode_controller.register("image_to_video", self._release_pipeline_locked)

    @property
    def pipeline_loaded(self) -> bool:
        return self._pipeline is not None

    def invalidate_pipeline(self) -> None:
        """Drop resident I2V GPU state so a transient retry rebuilds from local weights."""

        with self._lock:
            was_loaded = self._pipeline is not None
            self._release_pipeline_locked()
        logger.warning("LTX25_I2V_PIPELINE_INVALIDATED was_loaded=%s", was_loaded)

    def prepare(self) -> None:
        """Load and retain the validated LTX pipeline before queue traffic is accepted."""

        prepare_started = time.monotonic()
        require_runtime_bootstrap_complete()
        with self._lock:
            bindings = self._get_bindings()
            try:
                self._validate_runtime(bindings)
            except FileNotFoundError as exc:
                raise ModelBootstrapPendingError(str(exc)) from exc
            with _torch_inference_context(bindings.torch):
                self._get_or_build_pipeline(bindings)
            self._prepared = True
        logger.info(
            "LTX25_I2V_PREPARED prepare_seconds=%.3f pipeline_loaded=%s",
            time.monotonic() - prepare_started,
            self.pipeline_loaded,
        )

    def ready(self) -> None:
        """Check prepared dependencies without waiting on a long GPU generation.

        The same mode lock still serializes model construction and inference.
        A readiness probe must not acquire it: a 100-second A2V job otherwise
        causes repeated five-second probe timeouts and removes this instance
        from Salad job-queue dispatch midway through the job.
        """
        require_runtime_bootstrap_complete()
        bindings = self._bindings
        if not self._prepared or bindings is None:
            raise RuntimeError("LTX-2.5 pipeline has not been prepared")
        self._validate_runtime(bindings)

    def generate(
        self,
        *,
        keyframe_path: Path,
        output_path: Path,
        parameters: LTXVideoParameters,
    ) -> None:
        if not keyframe_path.is_file():
            raise FileNotFoundError(f"LTX keyframe input does not exist: {keyframe_path}")

        generation_started = time.monotonic()
        requested_backend = (
            "triton"
            if parameters.generation_profile
            in (
                LTX_CFG_BATCH2_TRITON_PROFILE,
                LTX_FAST_LOWRES_TRITON_PROFILE,
                LTX_FAST_FULLRES_TRITON_PROFILE,
            )
            else "eager"
        )
        requested_offload = (
            "none" if parameters.generation_profile in (
                LTX_FAST_LOWRES_NO_OFFLOAD_PROFILE,
                *LTX_FULLRES_NO_OFFLOAD_PROFILES,
            )
            else "cpu"
        )
        requested_quantization = (
            "nvfp4_prequant" if parameters.generation_profile == LTX_FAST_FULLRES_NVFP4_PROFILE
            else "fp8_cast"
        )
        fast_recipe = parameters.generation_profile in LTX_FAST_PROFILES
        lowres_recipe = parameters.generation_profile in LTX_FAST_LOWRES_PROFILES
        stage_1_batch = 2 if parameters.generation_profile in LTX_EXPERIMENTAL_PROFILES else 1
        # I2V never writes an audio stream: the final audio decoder runs only
        # after both visual diffusion stages and its output is discarded.
        # Preserve the old audio-enabled CFG-b2 profile as an explicit control.
        decode_audio = parameters.generation_profile == LTX_CFG_BATCH2_PROFILE
        with self._lock:
            pipeline_reused = (
                self._pipeline is not None
                and self._pipeline_requested_diffvae_backend == requested_backend
                and self._pipeline_requested_offload_mode == requested_offload
                and self._pipeline_requested_quantization == requested_quantization
            )

            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            with _torch_inference_context(bindings.torch):
                metrics = I2VPhaseMetrics(bindings.torch, self._device)
                metrics.begin_generation()
                with metrics.phase("pipeline_acquire_seconds"):
                    pipeline_kwargs_for_load = {"requested_diffvae_backend": requested_backend}
                    if requested_offload != "cpu":
                        pipeline_kwargs_for_load["requested_offload_mode"] = requested_offload
                    if requested_quantization != "fp8_cast":
                        pipeline_kwargs_for_load["requested_quantization"] = requested_quantization
                    pipeline = self._get_or_build_pipeline(
                        bindings, **pipeline_kwargs_for_load
                    )
                pipeline_prior_successful_generations = self._pipeline_successful_generations
                pipeline_width, pipeline_height = (
                    (1024, 576) if lowres_recipe else _pipeline_dimensions(parameters)
                )
                conditioning_path = keyframe_path
                if lowres_recipe:
                    from .fast_delivery import prepare_fast_keyframe

                    if (parameters.width, parameters.height) != (1280, 720):
                        raise ValueError("Fast 576p profiles require public 1280x720 delivery")
                    conditioning_path = prepare_fast_keyframe(
                        keyframe_path, output_path.parent / "keyframe_fast_1024x576.png"
                    )
                elif (pipeline_width, pipeline_height) != (
                    parameters.width, parameters.height
                ):
                    conditioning_path = _prepare_grid_keyframe(
                        keyframe_path,
                        output_path.parent / "keyframe_ltx_grid.png",
                        requested_width=parameters.width,
                        requested_height=parameters.height,
                        pipeline_width=pipeline_width,
                        pipeline_height=pipeline_height,
                    )
                conditioning = bindings.image_conditioning_input(
                    path=str(conditioning_path.resolve()),
                    frame_idx=0,
                    strength=1.0,
                    crf=None,
                )
                pipeline_kwargs = {
                    "prompt": parameters.prompt,
                    "seed": parameters.seed,
                    "height": pipeline_height,
                    "width": pipeline_width,
                    "frame_rate": float(parameters.fps),
                    "images": [conditioning],
                    "num_frames": parameters.num_frames,
                }
                if fast_recipe:
                    if parameters.generation_profile == LTX_FAST_VANILLA_REFERENCE_PROFILE:
                        # Pinned upstream reference, including its discarded final
                        # audio decode; only used to verify custom vanilla equivalence.
                        with metrics.phase("pipeline_call_seconds"):
                            result = pipeline(**pipeline_kwargs)
                    else:
                        # The pinned upstream 8+3 vanilla recipe, instrumented and
                        # skipping ONLY its unused final audio waveform decode.
                        from .fast_i2v import generate_fast_distilled

                        with metrics.phase("pipeline_call_seconds"):
                            result = generate_fast_distilled(
                                pipeline,
                                decode_audio=False,
                                metrics=metrics,
                                profile_prompt_subphases=parameters.generation_profile in (
                                    LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE,
                                    LTX_FAST_FULLRES_TRITON_PROFILE,
                                    LTX_FAST_FULLRES_NVFP4_PROFILE,
                                ),
                                **pipeline_kwargs,
                            )
                elif parameters.generation_profile == LTX_STATIC_CAMERA_GENERATION_PROFILE:
                    # Opt-in static-camera uses a distinct negative prompt.
                    generator = _load_static_camera_generator()
                    with metrics.phase("pipeline_call_seconds"):
                        result = generator(
                            pipeline, decode_audio=False, metrics=metrics, **pipeline_kwargs
                        )
                else:
                    generator, negative_prompt = _load_default_i2v_negative_guidance()
                    with metrics.phase("pipeline_call_seconds"):
                        result = generator(
                            pipeline,
                            negative_prompt=negative_prompt,
                            stage_1_max_batch_size=stage_1_batch,
                            decode_audio=decode_audio,
                            metrics=metrics,
                            **pipeline_kwargs,
                        )
                output_video = metrics.video_chunks(result.video)
                if not lowres_recipe and (pipeline_width, pipeline_height) != (
                    parameters.width, parameters.height
                ):
                    output_video = _crop_video_to_requested(
                        output_video,
                        requested_width=parameters.width,
                        requested_height=parameters.height,
                    )

                output_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path = (
                    output_path.with_name(output_path.stem + ".raw-576p.mp4")
                    if lowres_recipe else output_path
                )
                with metrics.phase("encode_including_decode_seconds"):
                    bindings.encode_video(
                        video=output_video,
                        fps=parameters.fps,
                        audio=None,
                        output_path=str(raw_path),
                        video_chunks_number=bindings.get_video_chunks_number(
                            result.num_frames,
                            result.tiling_config,
                        ),
                    )
                if lowres_recipe:
                    from .fast_delivery import deliver_fast_video

                    with metrics.phase("delivery_postprocess_seconds"):
                        deliver_fast_video(raw_path, output_path)
                    raw_path.unlink(missing_ok=True)
                metrics.timings["encode_excluding_video_decode_seconds"] = max(
                    0.0,
                    metrics.timings["encode_including_decode_seconds"]
                    - metrics.timings.get("video_decode_seconds", 0.0),
                )
                metrics.timings["total_generation_seconds"] = (
                    time.monotonic() - generation_started
                )
                self._pipeline_successful_generations += 1
                logger.info(
                    "LTX25_I2V_PHASE_METRICS %s",
                    json.dumps(
                        {
                            "generation_profile": parameters.generation_profile,
                            "diffvae_backend_requested": requested_backend,
                            "diffvae_backend_effective": self._pipeline_effective_diffvae_backend,
                            "stage1_max_batch_size": stage_1_batch,
                            "stage1_expected_forward_calls": (
                                8 if fast_recipe or stage_1_batch == 2 else 16
                            ),
                            "stage2_expected_forward_calls": 3,
                            "audio_decode_enabled": (
                                True if parameters.generation_profile
                                == LTX_FAST_VANILLA_REFERENCE_PROFILE
                                else False if fast_recipe else decode_audio
                            ),
                            "vanilla_simple_denoiser": fast_recipe,
                            "quantization": requested_quantization,
                            "offload_mode": requested_offload,
                            "internal_width": pipeline_width,
                            "internal_height": pipeline_height,
                            "delivery_width": parameters.width,
                            "delivery_height": parameters.height,
                            "frames": parameters.num_frames,
                            "fps": parameters.fps,
                            "pipeline_reused": pipeline_reused,
                            # Unlike pipeline_reused, a positive count proves that
                            # this exact pipeline already completed real inference.
                            "pipeline_prior_successful_generations": (
                                pipeline_prior_successful_generations
                            ),
                            "pipeline_known_warm": pipeline_prior_successful_generations > 0,
                            "cuda_peak_reset": metrics.cuda_peak_reset,
                            **metrics.timings,
                            **metrics.cuda_peak(),
                        },
                        sort_keys=True,
                    ),
                )
        logger.info(
            "LTX25_I2V_INFERENCE_METRIC generation_profile=%s num_frames=%d fps=%d "
            "total_seconds=%.3f pipeline_reused=%s",
            parameters.generation_profile,
            parameters.num_frames,
            parameters.fps,
            time.monotonic() - generation_started,
            pipeline_reused,
        )

    def _get_bindings(self) -> _LTXBindings:
        if self._bindings is None:
            self._bindings = _load_ltx_bindings()
        return self._bindings

    def _validate_runtime(self, bindings: _LTXBindings) -> None:
        self._model_files.validate()
        if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the LTX-2.5 production runtime")

    def _release_pipeline_locked(self) -> None:
        if self._pipeline is None:
            return
        self._pipeline = None
        self._pipeline_requested_diffvae_backend = None
        self._pipeline_effective_diffvae_backend = None
        self._pipeline_requested_offload_mode = "cpu"
        self._pipeline_requested_quantization = "fp8_cast"
        self._pipeline_successful_generations = 0
        gc.collect()
        if self._bindings is not None:
            cuda = getattr(self._bindings.torch, "cuda", None)
            empty_cache = getattr(cuda, "empty_cache", None)
            if empty_cache is not None:
                empty_cache()

    def _get_or_build_pipeline(
        self, bindings: _LTXBindings, *, requested_diffvae_backend: str = "eager",
        requested_offload_mode: str = "cpu",
        requested_quantization: str = "fp8_cast",
    ) -> Any:
        if requested_offload_mode not in ("cpu", "disk", "none"):
            raise ValueError("Unsupported LTX I2V offload mode")
        if requested_quantization not in ("fp8_cast", "nvfp4_prequant"):
            raise ValueError("Unsupported LTX I2V quantization")
        if requested_quantization == "nvfp4_prequant" and requested_offload_mode != "none":
            raise ValueError("NVFP4 prequant requires nonstreaming model residency")
        if self._pipeline is not None:
            if (self._pipeline_requested_diffvae_backend == requested_diffvae_backend
                    and self._pipeline_requested_offload_mode == requested_offload_mode
                    and self._pipeline_requested_quantization == requested_quantization):
                return self._pipeline
            self._release_pipeline_locked()

        load_started = time.monotonic()
        logger.info("LTX25_I2V_PIPELINE_LOAD_START device=%s", self._device)
        self._mode_controller.activate("image_to_video")
        transformer_path = self._model_files.transformer
        if requested_quantization == "nvfp4_prequant":
            from .nvfp4_runtime import require_nvfp4_runtime

            require_nvfp4_runtime(bindings.torch, self._device)
            transformer_path = (
                self._model_files.transformer.parent / _NVFP4_TRANSFORMER.rsplit("/", 1)[-1]
            )
            if not transformer_path.is_file():
                raise FileNotFoundError(
                    f"Opt-in NVFP4 checkpoint is missing: {transformer_path}. "
                    "Use the separate NVFP4 benchmark image; normal worker unchanged."
                )
        model_paths = bindings.model_paths.from_split(
            transformer_path=str(transformer_path),
            text_encoder_path=str(self._model_files.text_encoder),
            video_vae_path=str(self._model_files.video_vae),
            audio_vae_path=str(self._model_files.audio_vae),
        )
        if requested_quantization == "nvfp4_prequant":
            quantization = bindings.quantization_kind.NVFP4_PREQUANT.to_policy(
                checkpoint_path=str(transformer_path)
            )
        else:
            quantization = bindings.quantization_kind.FP8_CAST.to_policy(
                checkpoint_path=str(transformer_path)
            )
        effective_backend = _select_diffvae_backend(
            bindings, requested_diffvae_backend
        )
        self._pipeline = bindings.distilled_pipeline(
            model_paths=model_paths,
            spatial_upsampler_path=str(self._model_files.spatial_upsampler),
            loras=(),
            device=bindings.torch.device(self._device),
            quantization=quantization,
            offload_mode=(
                bindings.offload_mode.NONE if requested_offload_mode == "none"
                else bindings.offload_mode.DISK if requested_offload_mode == "disk"
                else bindings.offload_mode.CPU
            ),
        )
        self._pipeline_requested_offload_mode = requested_offload_mode
        self._pipeline_requested_quantization = requested_quantization
        self._pipeline_requested_diffvae_backend = requested_diffvae_backend
        self._pipeline_effective_diffvae_backend = effective_backend
        logger.info(
            "LTX25_I2V_PIPELINE_LOAD_DONE seconds=%.3f diffvae_backend=%s",
            time.monotonic() - load_started,
            effective_backend,
        )
        return self._pipeline




    def _pair_attempt(
        self,
        *,
        keyframe_a: Path,
        keyframe_b: Path,
        output_a: Path,
        output_b: Path,
        parameters_a: LTXVideoParameters,
        parameters_b: LTXVideoParameters,
        forward_batch_size: int,
        reuse_transformer_across_stages: bool = False,
    ) -> int | None:
        """Run one experimental attempt. Scoped so failed CUDA tensors can be released."""
        from .paired_i2v import PairInput, generate_paired_fast_distilled

        with self._lock:
            self.ready()
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            with _torch_inference_context(bindings.torch):
                cuda = bindings.torch.cuda
                if self._device.startswith("cuda"):
                    cuda.reset_peak_memory_stats()
                # Native 1080p B=2 exhausted RTX 5090 VRAM inside the stage2
                # transformer. Block-streaming FP8 uses the exact same weights,
                # forward batch size, sigmas and independent random generators.
                # DISK keeps only two pinned CPU block slots, unlike RAM
                # streaming, so the 40-GiB Salad container is protected too.
                # The proven 720p B=2 path remains GPU-resident and unchanged.
                pair_offload_mode = (
                    "disk" if (parameters_a.width, parameters_a.height) == (1920, 1080)
                    else "none"
                )
                logger.info(
                    "LTX25_I2V_PAIR_MEMORY_POLICY resolution=%dx%d offload=%s "
                    "true_gpu_batch=%d",
                    parameters_a.width, parameters_a.height, pair_offload_mode,
                    forward_batch_size,
                )
                pipeline = self._get_or_build_pipeline(
                    bindings, requested_offload_mode=pair_offload_mode,
                )
                if pair_offload_mode == "disk" and not pipeline.stage._is_streaming:
                    raise RuntimeError("Full HD B=2 requires block-streamed FP8 transformer")
                pipeline_width, pipeline_height = _pipeline_dimensions(parameters_a)
                pair = []
                for keyframe, output, params in (
                    (keyframe_a, output_a, parameters_a),
                    (keyframe_b, output_b, parameters_b),
                ):
                    destination = output.parent / (output.stem + ".batch-grid.png")
                    image_path = _prepare_grid_keyframe(
                        keyframe, destination,
                        requested_width=params.width,
                        requested_height=params.height,
                        pipeline_width=pipeline_width, pipeline_height=pipeline_height,
                    )
                    pair.append(PairInput(
                        params.prompt, params.seed,
                        bindings.image_conditioning_input(
                            path=str(image_path.resolve()), frame_idx=0,
                            strength=1.0, crf=None,
                        ),
                    ))
                results = generate_paired_fast_distilled(
                    pipeline, pair=(pair[0], pair[1]),
                    width=pipeline_width, height=pipeline_height,
                    frame_rate=float(parameters_a.fps),
                    num_frames=parameters_a.num_frames,
                    max_forward_batch_size=forward_batch_size,
                    reuse_transformer_across_stages=reuse_transformer_across_stages,
                )
                for result, output, params in zip(
                    results, (output_a, output_b),
                    (parameters_a, parameters_b), strict=True,
                ):
                    video = (
                        _crop_video_to_requested(
                            result.video,
                            requested_width=params.width,
                            requested_height=params.height,
                        )
                        if (pipeline_width, pipeline_height)
                        != (params.width, params.height)
                        else result.video
                    )
                    output.parent.mkdir(parents=True, exist_ok=True)
                    bindings.encode_video(
                        video=video, fps=params.fps, audio=None,
                        output_path=str(output),
                        video_chunks_number=bindings.get_video_chunks_number(
                            result.num_frames, result.tiling_config,
                        ),
                    )
                peak = (
                    cuda.max_memory_allocated() if self._device.startswith("cuda") else None
                )
                self._pipeline_successful_generations += 2
                return peak

    def generate_pair(
        self,
        *,
        keyframe_a: Path,
        keyframe_b: Path,
        output_a: Path,
        output_b: Path,
        parameters_a: LTXVideoParameters,
        parameters_b: LTXVideoParameters,
        allow_sequential_fallback: bool = False,
        reuse_transformer_across_stages: bool = False,
        accept_full_hd_batch2_oom_risk: bool = False,
    ) -> dict[str, Any]:
        """EXPERIMENT: B=2, then shared-weight B=1, then EXACT existing B=1.

        All recovery modes are explicit in the report, so a fallback can
        NEVER be mistaken for validated parallel GPU generation.
        """
        from ai_video_factory.inference.gpu_failures import (
            classify_gpu_failure,
            cleanup_cuda_memory,
        )

        from .resource_metrics import cuda_memory_snapshot, memory_pressure

        for path in (keyframe_a, keyframe_b):
            if not path.is_file():
                raise FileNotFoundError(f"Batch2 input not found: {path}")
        if (parameters_a.generation_profile, parameters_b.generation_profile) != (
            LTX_PRODUCTION_I2V_PROFILE, LTX_PRODUCTION_I2V_PROFILE,
        ):
            raise ValueError("Pairing uses only the validated FP8 no-offload profile")
        def geometry(p: LTXVideoParameters) -> tuple[int, int, int, int]:
            return p.width, p.height, p.fps, p.num_frames

        dimensions = geometry(parameters_a)
        if dimensions != geometry(parameters_b):
            raise ValueError("Batch2 experiment requires matching per-video dimensions")
        if dimensions == (1920, 1080, 24, 121):
            if not accept_full_hd_batch2_oom_risk:
                raise ValueError(
                    "1080p batch2 has not been GPU-validated: "
                    "explicitly accept the OOM risk"
                )
            if allow_sequential_fallback or reuse_transformer_across_stages:
                raise ValueError(
                    "Full HD trial must be real B2 with separate transformer "
                    "builds and no silent sequential fallback"
                )
        elif dimensions != (1280, 720, 24, 121):
            raise ValueError(
                "Batch2 experiment supports two matching 5s 720p/24fps videos, "
                "or an explicit-risk trial with two 5s 1080p/24fps videos"
            )
        if parameters_a.seed == parameters_b.seed:
            raise ValueError("Separate videos must have separate RNG seeds")

        started = time.monotonic()
        failed_attempts: list[dict[str, Any]] = []
        outputs = (output_a, output_b)

        # A one-build B=2 attempt holds the same GPU transformer across
        # stage1, upsample and stage2. If upsample/conditioning cannot fit
        # alongside it, retry the previously VISUALLY APPROVED two-build B=2
        # kernel automatically before trying any B=1 fallback.
        candidates = (
            [(2, True), (2, False)] if reuse_transformer_across_stages
            else [(2, False)]
        )
        if allow_sequential_fallback:
            candidates.append((1, False))
        for index, (forward_batch, shared_stages) in enumerate(candidates):
            label = "batch2" if forward_batch == 2 else "stage_shared_sequential_forward"
            attempt_started = time.monotonic()
            retry_after_oom = False
            try:
                peak = self._pair_attempt(
                    keyframe_a=keyframe_a, keyframe_b=keyframe_b,
                    output_a=output_a, output_b=output_b,
                    parameters_a=parameters_a, parameters_b=parameters_b,
                    forward_batch_size=forward_batch,
                    reuse_transformer_across_stages=shared_stages,
                )
            except Exception as exc:
                failure = classify_gpu_failure(exc)
                logger.warning(
                    "LTX25_I2V_PAIR_ATTEMPT_FAILED %s",
                    json.dumps({
                        "mode": label,
                        "shared_between_stages": shared_stages,
                        "failure": failure,
                        "exception_type": type(exc).__name__,
                        "elapsed_seconds": time.monotonic() - attempt_started,
                        "cuda": cuda_memory_snapshot(),
                        "system": memory_pressure(),
                    }, sort_keys=True),
                )
                failed_attempts.append({
                    "mode": label,
                    "shared_between_stages": shared_stages,
                    "gpu_failure": failure,
                    "error_type": type(exc).__name__,
                    "elapsed_seconds": time.monotonic() - attempt_started,
                })
                for output in outputs:
                    output.unlink(missing_ok=True)
                # Do not run gc.collect()/empty_cache while the live
                # exception traceback still owns stage locals and potentially
                # the previous GPU transformer. Cleanup after the except
                # scope ends, before starting any smaller-memory retry.
                if failure != "oom":
                    raise
                logger.warning(
                    "LTX25_I2V_PAIR_OOM mode=%s shared_between_stages=%s next_attempt=%s",
                    label, shared_stages,
                    candidates[index + 1] if index + 1 < len(candidates) else None,
                )
                # No fallback means no silent downgrade to sequential video
                # generation, but retrying B=2 WITHOUT shared model residency
                # preserves the previously validated GPU-parallel algorithm.
                if index + 1 == len(candidates) and not allow_sequential_fallback:
                    raise
                retry_after_oom = True
            else:
                report = {
                    "mode": label,
                    "actual_gpu_batch_size": forward_batch,
                    "shared_transformer_stage_builds": 1 if shared_stages else 2,
                    "transformer_reused_between_stages": shared_stages,
                    "quality": "visual_review_pending",
                    "requested_width": dimensions[0],
                    "requested_height": dimensions[1],
                    "full_hd_gpu_validation_pending": dimensions[1] == 1080,
                    "transformer_offload_mode": "disk" if dimensions[1] == 1080 else "none",
                    "recipe": LTX_PRODUCTION_I2V_PROFILE,
                    "individual_seeds": [parameters_a.seed, parameters_b.seed],
                    "elapsed_seconds": time.monotonic() - started,
                    "peak_vram_bytes": peak,
                    "failed_attempts": failed_attempts,
                }
                logger.info("LTX25_I2V_PAIR_METRICS %s", json.dumps(report, sort_keys=True))
                return report
            if retry_after_oom:
                self.invalidate_pipeline()
                cleanup_cuda_memory()

        # Last-resort fallback: EXACTLY the previously visually approved B=1
        # generator. At no point does one batch clip mix with one reference clip.
        for output in outputs:
            output.unlink(missing_ok=True)
        self.generate(
            keyframe_path=keyframe_a, output_path=output_a,
            parameters=parameters_a,
        )
        self.generate(
            keyframe_path=keyframe_b, output_path=output_b,
            parameters=parameters_b,
        )
        report = {
            "mode": "reference_sequential_oom_fallback",
            "actual_gpu_batch_size": 1,
            "shared_transformer_stage_builds": None,
            "quality": "existing_B1_recipe_visual_review_pending",
            "recipe": LTX_PRODUCTION_I2V_PROFILE,
            "individual_seeds": [parameters_a.seed, parameters_b.seed],
            "elapsed_seconds": time.monotonic() - started,
            "peak_vram_bytes": None,
            "failed_attempts": failed_attempts,
        }
        logger.info("LTX25_I2V_PAIR_METRICS %s", json.dumps(report, sort_keys=True))
        return report



class LTXVideoTaskRunner:
    """Task adapter for one storyboard-keyframe-conditioned LTX video clip."""

    task_name = LTX_VIDEO_TASK

    def __init__(self, *, backend: LTXVideoBackend) -> None:
        self._backend = backend

    def prepare(self) -> None:
        self._backend.prepare()

    def ready(self) -> None:
        self._backend.ready()

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        self._validate_request(request, inputs)
        parameters = LTXVideoParameters.model_validate(request.parameters)
        output = work_dir / "output.mp4"
        try:
            self._backend.generate(
                keyframe_path=inputs["keyframe"],
                output_path=output,
                parameters=parameters,
            )
        except Exception as exc:
            if is_retryable_gpu_failure(exc):
                invalidator = getattr(self._backend, "invalidate_pipeline", None)
                if callable(invalidator):
                    try:
                        invalidator()
                    except Exception:
                        logger.exception(
                            "failed to invalidate LTX I2V pipeline after transient GPU failure"
                        )
            raise
        if not output.is_file() or output.stat().st_size <= 0:
            raise ValueError("LTX-2.5 backend did not produce a non-empty MP4 artifact")
        return LocalArtifact(path=output, content_type="video/mp4")

    def _validate_request(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
    ) -> None:
        if request.task != self.task_name:
            raise ValueError(f"LTXVideoTaskRunner cannot execute task {request.task!r}")
        if set(inputs) != {"keyframe"}:
            raise ValueError("video.ltx25.generate requires exactly one 'keyframe' input")
        declared_names = {item.name for item in request.inputs}
        if declared_names != {"keyframe"}:
            raise ValueError("video.ltx25.generate request must declare one 'keyframe' input")
        if request.output.content_type != "video/mp4":
            raise ValueError("video.ltx25.generate output content_type must be 'video/mp4'")
        if Path(request.output.key).suffix.lower() != ".mp4":
            raise ValueError("video.ltx25.generate output key must end in .mp4")
