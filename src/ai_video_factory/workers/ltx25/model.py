from __future__ import annotations

import gc
import math
import threading
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact

LTX_VIDEO_TASK = "video.ltx25.generate"
LTX_GENERATION_PROFILE = "ltx25-distilled-a95ab856-fp8cpu-gridpad-eagersdpa-v4"
_CANONICAL_LANDSCAPE_SIZE = (1280, 720)
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
        if value != LTX_GENERATION_PROFILE:
            raise ValueError(
                f"generation_profile must be exactly {LTX_GENERATION_PROFILE!r}"
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
        if not on_native_grid and (self.width, self.height) != _CANONICAL_LANDSCAPE_SIZE:
            raise ValueError(
                "LTX-2.5 two-stage dimensions must be divisible by 64, except the "
                "canonical 1280x720 profile which uses deterministic internal grid padding"
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


def _prepare_grid_keyframe(
    source: Path,
    destination: Path,
    *,
    requested_width: int,
    requested_height: int,
    pipeline_width: int,
    pipeline_height: int,
) -> Path:
    """Resize same-aspect conditioning and extend only model-grid padding with edge pixels."""

    with Image.open(source) as opened:
        image = opened.convert("RGB")
    if image.width * requested_height != image.height * requested_width:
        raise ValueError(
            "LTX keyframe aspect ratio must match the requested video; refusing to stretch "
            f"{image.width}x{image.height} into {requested_width}x{requested_height}"
        )

    resized = image.resize((requested_width, requested_height), Image.Resampling.LANCZOS)
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


def _load_ltx_bindings() -> _LTXBindings:
    try:
        import torch
        from ltx_core.model.video_vae import get_video_chunks_number
        from ltx_core.model.video_vae.transformer import apply as diffvae_apply
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
    )


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
        self._mode_controller = mode_controller or LTXPipelineModeController()
        self._lock = self._mode_controller.lock
        self._mode_controller.register("image_to_video", self._release_pipeline_locked)

    @property
    def pipeline_loaded(self) -> bool:
        return self._pipeline is not None

    def prepare(self) -> None:
        """Load and retain the validated LTX pipeline before queue traffic is accepted."""

        with self._lock:
            bindings = self._get_bindings()
            try:
                self._validate_runtime(bindings)
            except FileNotFoundError as exc:
                raise ModelBootstrapPendingError(str(exc)) from exc
            with _torch_inference_context(bindings.torch):
                self._get_or_build_pipeline(bindings)

    def ready(self) -> None:
        """Verify the warmed runtime remains usable without rebuilding the pipeline."""

        with self._lock:
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            if self._pipeline is None:
                raise RuntimeError("LTX-2.5 pipeline has not been prepared")

    def generate(
        self,
        *,
        keyframe_path: Path,
        output_path: Path,
        parameters: LTXVideoParameters,
    ) -> None:
        if not keyframe_path.is_file():
            raise FileNotFoundError(f"LTX keyframe input does not exist: {keyframe_path}")

        with self._lock:
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            with _torch_inference_context(bindings.torch):
                pipeline = self._get_or_build_pipeline(bindings)
                pipeline_width, pipeline_height = _pipeline_dimensions(parameters)
                conditioning_path = keyframe_path
                if (pipeline_width, pipeline_height) != (
                    parameters.width,
                    parameters.height,
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
                result = pipeline(
                    prompt=parameters.prompt,
                    seed=parameters.seed,
                    height=pipeline_height,
                    width=pipeline_width,
                    frame_rate=float(parameters.fps),
                    images=[conditioning],
                    num_frames=parameters.num_frames,
                )
                output_video = result.video
                if (pipeline_width, pipeline_height) != (
                    parameters.width,
                    parameters.height,
                ):
                    output_video = _crop_video_to_requested(
                        output_video,
                        requested_width=parameters.width,
                        requested_height=parameters.height,
                    )

                output_path.parent.mkdir(parents=True, exist_ok=True)
                bindings.encode_video(
                    video=output_video,
                    fps=parameters.fps,
                    audio=None,
                    output_path=str(output_path),
                    video_chunks_number=bindings.get_video_chunks_number(
                        result.num_frames,
                        result.tiling_config,
                    ),
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
        gc.collect()
        if self._bindings is not None:
            cuda = getattr(self._bindings.torch, "cuda", None)
            empty_cache = getattr(cuda, "empty_cache", None)
            if empty_cache is not None:
                empty_cache()

    def _get_or_build_pipeline(self, bindings: _LTXBindings) -> Any:
        if self._pipeline is not None:
            return self._pipeline

        self._mode_controller.activate("image_to_video")
        model_paths = bindings.model_paths.from_split(
            transformer_path=str(self._model_files.transformer),
            text_encoder_path=str(self._model_files.text_encoder),
            video_vae_path=str(self._model_files.video_vae),
            audio_vae_path=str(self._model_files.audio_vae),
        )
        quantization = bindings.quantization_kind.FP8_CAST.to_policy(
            checkpoint_path=str(self._model_files.transformer)
        )
        _force_diffvae_eager_sdpa(bindings.diffvae_apply)
        self._pipeline = bindings.distilled_pipeline(
            model_paths=model_paths,
            spatial_upsampler_path=str(self._model_files.spatial_upsampler),
            loras=(),
            device=bindings.torch.device(self._device),
            quantization=quantization,
            offload_mode=bindings.offload_mode.CPU,
        )
        return self._pipeline


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
        self._backend.generate(
            keyframe_path=inputs["keyframe"],
            output_path=output,
            parameters=parameters,
        )
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
