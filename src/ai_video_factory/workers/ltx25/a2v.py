from __future__ import annotations

import gc
import json
import logging
import math
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact, LocalSidecarArtifact

from .model import (
    LTXModelFiles,
    LTXPipelineModeController,
    _crop_video_to_requested,
    _force_diffvae_eager_sdpa,
    _pipeline_dimensions,
)

logger = logging.getLogger(__name__)

LTX_A2V_TASK = "video.ltx25.audio_to_video"
LTX_A2V_GENERATION_PROFILE = "ltx25-a2v-dev-a95ab856-fp8cpu-gridpad-eagersdpa-v1"
LTX_A2V_RECOMMENDED_MAX_SECONDS = 12.0
LTX_A2V_MAX_RAW_FRAMES = 1024
LTX_A2V_DEFAULT_PROMPT = (
    "A medium close-up talking head of the person in the reference image, speaking "
    "naturally toward the camera in synchronization with the provided speech audio. "
    "Preserve the person's facial identity, hairstyle, skin tone, clothing, background, "
    "and framing throughout the continuous shot. Natural blinking and restrained facial, "
    "mouth, jaw, and head movement. Static camera, stable lighting, no cuts, no scene "
    "changes, no exaggerated gestures, no identity drift, and no facial deformation."
)

_DEV_TRANSFORMER = "diffusion_models/ltx-2.5-22b-dev-transformer-bf16.safetensors"
_DISTILLED_LORA = "loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors"


class LTXAudioToVideoParameters(BaseModel):
    """Audio-driven LTX-2.5 parameters. Audio length owns the temporal contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str = LTX_A2V_GENERATION_PROFILE
    prompt: str = Field(default=LTX_A2V_DEFAULT_PROMPT, min_length=1, max_length=8000)
    seed: int = Field(default=42, ge=0, le=2**63 - 1)
    width: int = Field(default=1280, gt=0, le=8192)
    height: int = Field(default=720, gt=0, le=8192)
    fps: int = Field(default=24, gt=0, le=120)

    @field_validator("generation_profile")
    @classmethod
    def validate_generation_profile(cls, value: str) -> str:
        if value != LTX_A2V_GENERATION_PROFILE:
            raise ValueError(
                f"generation_profile must be exactly {LTX_A2V_GENERATION_PROFILE!r}"
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
    def validate_shape(self) -> LTXAudioToVideoParameters:
        if (self.width, self.height) != (1280, 720):
            if self.width % 64 != 0 or self.height % 64 != 0:
                raise ValueError(
                    "LTX-2.5 two-stage dimensions must be divisible by 64, except the "
                    "canonical 1280x720 profile which uses deterministic internal grid padding"
                )
        return self


@dataclass(frozen=True, slots=True)
class LTXA2VModelFiles:
    shared: LTXModelFiles
    dev_transformer: Path
    distilled_lora: Path

    @classmethod
    def from_root(cls, root: Path) -> LTXA2VModelFiles:
        return cls(
            shared=LTXModelFiles.from_root(root),
            dev_transformer=root / _DEV_TRANSFORMER,
            distilled_lora=root / _DISTILLED_LORA,
        )

    def validate(self) -> None:
        paths = (*self.shared.paths(), self.dev_transformer, self.distilled_lora)
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Missing LTX-2.5 A2V model files: " + ", ".join(missing)
            )


@dataclass(frozen=True, slots=True)
class AudioProbe:
    codec: str
    sample_rate: int
    channels: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class _A2VBindings:
    torch: Any
    a2v_pipeline: Any
    model_paths: Any
    quantization_kind: Any
    offload_mode: Any
    image_conditioning_input: Any
    encode_video: Any
    get_video_chunks_number: Any
    diffvae_apply: Any
    lora_tuple: Any
    lora_sd_ops: Any
    detect_params: Any
    default_negative_prompt: str


def _load_a2v_bindings() -> _A2VBindings:
    try:
        import torch
        from ltx_core.loader import (
            LTXV_LORA_COMFY_RENAMING_MAP,
            LoraPathStrengthAndSDOps,
        )
        from ltx_core.model.video_vae import get_video_chunks_number
        from ltx_core.model.video_vae.transformer import apply as diffvae_apply
        from ltx_pipelines.a2vid_two_stage import A2VidPipelineTwoStage
        from ltx_pipelines.utils.args import ImageConditioningInput
        from ltx_pipelines.utils.constants import DEFAULT_NEGATIVE_PROMPT, detect_params
        from ltx_pipelines.utils.media_io import encode_video
        from ltx_pipelines.utils.model_paths import ModelPaths
        from ltx_pipelines.utils.quantization_factory import QuantizationKind
        from ltx_pipelines.utils.types import OffloadMode
    except ImportError as exc:
        raise RuntimeError(
            "LTX-2.5 A2V runtime dependencies are not installed in this environment"
        ) from exc

    return _A2VBindings(
        torch=torch,
        a2v_pipeline=A2VidPipelineTwoStage,
        model_paths=ModelPaths,
        quantization_kind=QuantizationKind,
        offload_mode=OffloadMode,
        image_conditioning_input=ImageConditioningInput,
        encode_video=encode_video,
        get_video_chunks_number=get_video_chunks_number,
        diffvae_apply=diffvae_apply,
        lora_tuple=LoraPathStrengthAndSDOps,
        lora_sd_ops=LTXV_LORA_COMFY_RENAMING_MAP,
        detect_params=detect_params,
        default_negative_prompt=DEFAULT_NEGATIVE_PROMPT,
    )


def _ffprobe_json(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise RuntimeError("ffprobe is required by the LTX A2V worker")
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def probe_audio(path: Path) -> AudioProbe:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"invalid audio input: {path}")
    try:
        probe = _ffprobe_json(path)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError(f"audio decode failed for {path}") from exc

    streams = [
        stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"
    ]
    if not streams:
        raise ValueError("audio input does not contain a decodable audio stream")
    stream = streams[0]
    duration_value = (
        stream.get("duration")
        or (probe.get("format") or {}).get("duration")
        or "0"
    )
    try:
        duration = float(duration_value)
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("audio probe returned invalid stream metadata") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("audio duration must be a positive finite number")
    if sample_rate <= 0 or channels <= 0:
        raise ValueError("audio stream must report positive sample rate and channel count")
    return AudioProbe(
        codec=str(stream.get("codec_name") or "unknown"),
        sample_rate=sample_rate,
        channels=channels,
        duration_seconds=duration,
    )


def _validate_audio_duration(audio: AudioProbe, *, fps: int) -> None:
    max_grid_frames = ((LTX_A2V_MAX_RAW_FRAMES - 1) // 8) * 8 + 1
    hard_max_seconds = max_grid_frames / float(fps)
    if audio.duration_seconds > hard_max_seconds:
        raise ValueError(
            "audio duration exceeds the LTX A2V temporal-grid hard maximum for "
            f"{fps} fps: {audio.duration_seconds:.3f}s > {hard_max_seconds:.3f}s"
        )


def _prepare_avatar_image(
    source: Path,
    destination: Path,
    *,
    requested_width: int,
    requested_height: int,
    pipeline_width: int,
    pipeline_height: int,
) -> Path:
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError(f"invalid avatar image input: {source}")
    try:
        with Image.open(source) as opened:
            image = opened.convert("RGB")
    except Exception as exc:
        raise ValueError(f"avatar image decode failed for {source}") from exc

    fitted = ImageOps.fit(
        image,
        (requested_width, requested_height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    pad_total = pipeline_height - requested_height
    if pipeline_width != requested_width or pad_total < 0:
        raise ValueError("unsupported LTX A2V grid-padding geometry")
    pad_top = pad_total // 2
    pad_bottom = pad_total - pad_top
    canvas = Image.new("RGB", (pipeline_width, pipeline_height))
    canvas.paste(fitted, (0, pad_top))
    if pad_top:
        top_row = fitted.crop((0, 0, requested_width, 1)).resize(
            (requested_width, pad_top)
        )
        canvas.paste(top_row, (0, 0))
    if pad_bottom:
        bottom_row = fitted.crop(
            (0, requested_height - 1, requested_width, requested_height)
        ).resize((requested_width, pad_bottom))
        canvas.paste(bottom_row, (0, pad_top + requested_height))
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, format="PNG")
    return destination


def _result_audio_duration(audio: Any) -> float:
    waveform = getattr(audio, "waveform", None)
    sample_rate = int(getattr(audio, "sampling_rate", 0) or 0)
    shape = getattr(waveform, "shape", None)
    if shape is None or not shape or sample_rate <= 0:
        raise RuntimeError("LTX A2V returned audio without measurable duration")
    return int(shape[-1]) / float(sample_rate)


def _output_duration(path: Path) -> float:
    probe = _ffprobe_json(path)
    value = (probe.get("format") or {}).get("duration")
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("generated MP4 does not report a valid duration") from exc
    if duration <= 0:
        raise RuntimeError("generated MP4 duration must be positive")
    return duration


class LTXAudioToVideoBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def generate(
        self,
        *,
        image_path: Path,
        audio_path: Path,
        output_path: Path,
        parameters: LTXAudioToVideoParameters,
    ) -> dict[str, Any]: ...


class DirectLTX25AudioToVideoBackend:
    """Official A2VidPipelineTwoStage adapter sharing one resident-mode controller."""

    def __init__(
        self,
        *,
        model_root: Path,
        device: str = "cuda",
        mode_controller: LTXPipelineModeController | None = None,
    ) -> None:
        self._model_files = LTXA2VModelFiles.from_root(model_root)
        self._device = device
        self._mode_controller = mode_controller or LTXPipelineModeController()
        self._lock = self._mode_controller.lock
        self._bindings: _A2VBindings | None = None
        self._pipeline: Any | None = None
        self._pipeline_params: Any | None = None
        self._mode_controller.register("audio_to_video", self._release_pipeline_locked)

    @property
    def pipeline_loaded(self) -> bool:
        return self._pipeline is not None

    def prepare(self) -> None:
        # Validate every A2V asset during readiness, but do not keep a second 22B pipeline
        # resident beside the already-warmed I2V pipeline.
        with self._lock:
            bindings = self._get_bindings()
            try:
                self._validate_runtime(bindings)
            except FileNotFoundError as exc:
                raise ModelBootstrapPendingError(str(exc)) from exc
            self._pipeline_params = bindings.detect_params(
                str(self._model_files.dev_transformer)
            )

    def ready(self) -> None:
        with self._lock:
            self._validate_runtime(self._get_bindings())

    def generate(
        self,
        *,
        image_path: Path,
        audio_path: Path,
        output_path: Path,
        parameters: LTXAudioToVideoParameters,
    ) -> dict[str, Any]:
        audio_probe = probe_audio(audio_path)
        _validate_audio_duration(audio_probe, fps=parameters.fps)

        with self._lock:
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            pipeline_width, pipeline_height = _pipeline_dimensions(parameters)
            conditioning_path = _prepare_avatar_image(
                image_path,
                output_path.parent / "avatar_ltx_grid.png",
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

            build_started = time.monotonic()
            pipeline, built_now = self._get_or_build_pipeline(bindings)
            model_load_seconds = time.monotonic() - build_started if built_now else 0.0

            cuda = getattr(bindings.torch, "cuda", None)
            reset_peak = getattr(cuda, "reset_peak_memory_stats", None)
            if reset_peak is not None:
                reset_peak()

            inference_started = time.monotonic()
            result = pipeline(
                prompt=parameters.prompt,
                negative_prompt=bindings.default_negative_prompt,
                seed=parameters.seed,
                height=pipeline_height,
                width=pipeline_width,
                num_frames=None,
                frame_rate=float(parameters.fps),
                num_inference_steps=self._pipeline_params.num_inference_steps,
                video_guider_params=self._pipeline_params.video_guider_params,
                images=[conditioning],
                audio_path=str(audio_path.resolve()),
                audio_start_time=0.0,
                audio_max_duration=None,
            )
            inference_seconds = time.monotonic() - inference_started

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
            encode_started = time.monotonic()
            bindings.encode_video(
                video=output_video,
                fps=parameters.fps,
                audio=result.audio,
                output_path=str(output_path),
                video_chunks_number=bindings.get_video_chunks_number(
                    result.num_frames,
                    result.tiling_config,
                ),
            )
            encode_seconds = time.monotonic() - encode_started

            peak_vram_bytes = None
            max_memory = getattr(cuda, "max_memory_allocated", None)
            if max_memory is not None:
                peak_vram_bytes = int(max_memory())

        effective_audio_duration = _result_audio_duration(result.audio)
        output_video_duration = _output_duration(output_path)
        total_seconds = model_load_seconds + inference_seconds + encode_seconds
        return {
            "generation_mode": "audio_to_video",
            "generation_profile": parameters.generation_profile,
            "input_audio_codec": audio_probe.codec,
            "input_audio_sample_rate": audio_probe.sample_rate,
            "input_audio_channels": audio_probe.channels,
            "input_audio_duration_seconds": audio_probe.duration_seconds,
            "effective_audio_duration_seconds": effective_audio_duration,
            "output_video_duration_seconds": output_video_duration,
            "duration_delta_seconds": output_video_duration - audio_probe.duration_seconds,
            "fps": parameters.fps,
            "num_frames": int(result.num_frames),
            "width": parameters.width,
            "height": parameters.height,
            "seed": parameters.seed,
            "model_load_seconds": model_load_seconds,
            "inference_seconds": inference_seconds,
            "video_encode_mux_seconds": encode_seconds,
            "generation_elapsed_seconds": total_seconds,
            "real_time_factor": inference_seconds / audio_probe.duration_seconds,
            "peak_vram_bytes": peak_vram_bytes,
            "pipeline_reused": not built_now,
            "recommended_max_duration_seconds": LTX_A2V_RECOMMENDED_MAX_SECONDS,
        }

    def _get_bindings(self) -> _A2VBindings:
        if self._bindings is None:
            self._bindings = _load_a2v_bindings()
        return self._bindings

    def _validate_runtime(self, bindings: _A2VBindings) -> None:
        self._model_files.validate()
        if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the LTX-2.5 A2V runtime")

    def _release_pipeline_locked(self) -> None:
        if self._pipeline is None:
            return
        self._pipeline = None
        gc.collect()
        if self._bindings is not None:
            empty_cache = getattr(self._bindings.torch.cuda, "empty_cache", None)
            if empty_cache is not None:
                empty_cache()

    def _get_or_build_pipeline(self, bindings: _A2VBindings) -> tuple[Any, bool]:
        if self._pipeline is not None:
            return self._pipeline, False

        self._mode_controller.activate("audio_to_video")
        model_paths = bindings.model_paths.from_split(
            transformer_path=str(self._model_files.dev_transformer),
            text_encoder_path=str(self._model_files.shared.text_encoder),
            video_vae_path=str(self._model_files.shared.video_vae),
            audio_vae_path=str(self._model_files.shared.audio_vae),
        )
        quantization = bindings.quantization_kind.FP8_CAST.to_policy(
            checkpoint_path=str(self._model_files.dev_transformer)
        )
        distilled_lora = [
            bindings.lora_tuple(
                str(self._model_files.distilled_lora),
                1.0,
                bindings.lora_sd_ops,
            )
        ]
        _force_diffvae_eager_sdpa(bindings.diffvae_apply)
        self._pipeline = bindings.a2v_pipeline(
            model_paths=model_paths,
            distilled_lora=distilled_lora,
            spatial_upsampler_path=str(self._model_files.shared.spatial_upsampler),
            loras=(),
            device=bindings.torch.device(self._device),
            quantization=quantization,
            offload_mode=bindings.offload_mode.CPU,
        )
        if self._pipeline_params is None:
            self._pipeline_params = bindings.detect_params(
                str(self._model_files.dev_transformer)
            )
        return self._pipeline, True


class LTXAudioToVideoTaskRunner:
    task_name = LTX_A2V_TASK

    def __init__(self, *, backend: LTXAudioToVideoBackend) -> None:
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
        parameters = LTXAudioToVideoParameters.model_validate(request.parameters)
        output = work_dir / "output.mp4"
        metadata = self._backend.generate(
            image_path=inputs["image"],
            audio_path=inputs["audio"],
            output_path=output,
            parameters=parameters,
        )
        if not output.is_file() or output.stat().st_size <= 0:
            raise ValueError("LTX-2.5 A2V backend did not produce a non-empty MP4 artifact")

        metadata_path = work_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return LocalArtifact(
            path=output,
            content_type="video/mp4",
            sidecars=(
                LocalSidecarArtifact(
                    name="metadata",
                    path=metadata_path,
                    content_type="application/json",
                ),
            ),
        )

    def _validate_request(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
    ) -> None:
        if request.task != self.task_name:
            raise ValueError(
                f"LTXAudioToVideoTaskRunner cannot execute task {request.task!r}"
            )
        if set(inputs) != {"image", "audio"}:
            raise ValueError(
                "video.ltx25.audio_to_video requires exactly 'image' and 'audio' inputs"
            )
        declared_names = {item.name for item in request.inputs}
        if declared_names != {"image", "audio"}:
            raise ValueError(
                "video.ltx25.audio_to_video request must declare 'image' and 'audio' inputs"
            )
        if request.output.content_type != "video/mp4":
            raise ValueError("LTX A2V output content_type must be 'video/mp4'")
        if Path(request.output.key).suffix.lower() != ".mp4":
            raise ValueError("LTX A2V output key must end in .mp4")
        sidecars = request.sidecar_outputs or {}
        if set(sidecars) != {"metadata"}:
            raise ValueError("LTX A2V request must declare exactly one 'metadata' sidecar")
        metadata_output = sidecars["metadata"]
        if metadata_output.content_type != "application/json":
            raise ValueError("LTX A2V metadata sidecar must use application/json")
        if Path(metadata_output.key).suffix.lower() != ".json":
            raise ValueError("LTX A2V metadata sidecar key must end in .json")
