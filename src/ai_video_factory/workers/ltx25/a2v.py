from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact, LocalArtifactPart

from .model import (
    _CANONICAL_LANDSCAPE_SIZE,
    _LTX_TWO_STAGE_SPATIAL_GRID,
    _crop_video_to_requested,
    _force_diffvae_eager_sdpa,
    _round_up_to_grid,
)

LTX_A2V_TASK = "video.ltx25.audio_to_video"
LTX_A2V_GENERATION_PROFILE = "ltx25-a2v-dev-a95ab856-fp8cpu-gridpad-eagersdpa-v1"
LTX_A2V_DEFAULT_PROMPT = (
    "A medium close-up talking head of the person in the reference image. "
    "The person looks naturally toward the camera and speaks in precise synchronization "
    "with the provided speech audio. Preserve facial identity, hairstyle, skin tone, "
    "clothing, body proportions, framing, and background. Natural blinking, subtle facial "
    "expressions, restrained head movement and realistic mouth articulation. Static camera, "
    "single continuous shot, no cuts, no scene changes, no camera movement, no exaggerated "
    "gestures, no identity drift."
)
LTX_A2V_RECOMMENDED_MAX_SECONDS: float | None = None
LTX_A2V_HARD_MAX_SECONDS: float | None = None

_DEV_TRANSFORMER = "diffusion_models/ltx-2.5-22b-dev-transformer-bf16.safetensors"
_TEXT_ENCODER = "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors"
_VIDEO_VAE = "vae/ltx-2.5-video-vae-bf16.safetensors"
_AUDIO_VAE = "vae/ltx-2.5-audio-vae-bf16.safetensors"
_SPATIAL_UPSAMPLER = (
    "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"
)
_DISTILLED_LORA = "loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors"


class LTXA2VParameters(BaseModel):
    """Validated audio-driven LTX-2.5 generation parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str = LTX_A2V_GENERATION_PROFILE
    prompt: str = Field(default=LTX_A2V_DEFAULT_PROMPT, min_length=1, max_length=8000)
    seed: int = Field(default=10, ge=0, le=2**63 - 1)
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
    def validate_shape(self) -> LTXA2VParameters:
        on_native_grid = (
            self.width % _LTX_TWO_STAGE_SPATIAL_GRID == 0
            and self.height % _LTX_TWO_STAGE_SPATIAL_GRID == 0
        )
        if not on_native_grid and (self.width, self.height) != _CANONICAL_LANDSCAPE_SIZE:
            raise ValueError(
                "LTX-2.5 A2V dimensions must be divisible by 64, except canonical 1280x720"
            )
        return self


@dataclass(frozen=True, slots=True)
class LTXA2VModelFiles:
    transformer: Path
    text_encoder: Path
    video_vae: Path
    audio_vae: Path
    spatial_upsampler: Path
    distilled_lora: Path

    @classmethod
    def from_root(cls, root: Path) -> LTXA2VModelFiles:
        return cls(
            transformer=root / _DEV_TRANSFORMER,
            text_encoder=root / _TEXT_ENCODER,
            video_vae=root / _VIDEO_VAE,
            audio_vae=root / _AUDIO_VAE,
            spatial_upsampler=root / _SPATIAL_UPSAMPLER,
            distilled_lora=root / _DISTILLED_LORA,
        )

    def paths(self) -> tuple[Path, ...]:
        return (
            self.transformer,
            self.text_encoder,
            self.video_vae,
            self.audio_vae,
            self.spatial_upsampler,
            self.distilled_lora,
        )

    def validate(self) -> None:
        missing = [str(path) for path in self.paths() if not path.is_file()]
        if missing:
            raise FileNotFoundError("Missing LTX-2.5 A2V model files: " + ", ".join(missing))


@dataclass(frozen=True, slots=True)
class AudioProbe:
    duration_seconds: float
    codec: str
    sample_rate: int
    channels: int


@dataclass(frozen=True, slots=True)
class VideoProbe:
    duration_seconds: float
    width: int
    height: int
    fps: float
    has_audio: bool


@dataclass(frozen=True, slots=True)
class A2VGenerationResult:
    num_frames: int
    effective_audio_duration_seconds: float
    output_video_duration_seconds: float
    model_load_seconds: float
    inference_seconds: float
    encode_seconds: float
    input_audio: AudioProbe
    output_video: VideoProbe


class LTXA2VBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def generate(
        self,
        *,
        avatar_image_path: Path,
        audio_path: Path,
        output_path: Path,
        parameters: LTXA2VParameters,
    ) -> A2VGenerationResult: ...


@dataclass(frozen=True, slots=True)
class _A2VBindings:
    torch: Any
    pipeline_class: Any
    model_paths: Any
    quantization_kind: Any
    offload_mode: Any
    image_conditioning_input: Any
    encode_video: Any
    get_video_chunks_number: Any
    multi_modal_guider_params: Any
    lora_path_strength_and_sd_ops: Any
    lora_sd_ops: Any
    default_negative_prompt: str
    auto_tiling: Any
    diffvae_apply: Any


def _load_a2v_bindings() -> _A2VBindings:
    try:
        import torch
        from ltx_core.components.guiders import MultiModalGuiderParams
        from ltx_core.loader import LTXV_LORA_COMFY_RENAMING_MAP, LoraPathStrengthAndSDOps
        from ltx_core.model.video_vae import AUTO_TILING, get_video_chunks_number
        from ltx_core.model.video_vae.transformer import apply as diffvae_apply
        from ltx_pipelines.a2vid_two_stage import A2VidPipelineTwoStage
        from ltx_pipelines.utils.args import ImageConditioningInput
        from ltx_pipelines.utils.constants import DEFAULT_NEGATIVE_PROMPT
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
        pipeline_class=A2VidPipelineTwoStage,
        model_paths=ModelPaths,
        quantization_kind=QuantizationKind,
        offload_mode=OffloadMode,
        image_conditioning_input=ImageConditioningInput,
        encode_video=encode_video,
        get_video_chunks_number=get_video_chunks_number,
        multi_modal_guider_params=MultiModalGuiderParams,
        lora_path_strength_and_sd_ops=LoraPathStrengthAndSDOps,
        lora_sd_ops=LTXV_LORA_COMFY_RENAMING_MAP,
        default_negative_prompt=DEFAULT_NEGATIVE_PROMPT,
        auto_tiling=AUTO_TILING,
        diffvae_apply=diffvae_apply,
    )


def _probe_json(path: Path) -> dict[str, Any]:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,sample_rate,channels,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or "ffprobe failed"
        raise ValueError(f"media decode failed for {path.name}: {detail}")
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe returned invalid JSON for {path.name}") from exc


def probe_audio(path: Path) -> AudioProbe:
    if not path.is_file():
        raise FileNotFoundError(f"audio input does not exist: {path}")
    payload = _probe_json(path)
    streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise ValueError("invalid audio: no decodable audio stream")
    stream = streams[0]
    try:
        duration = float(payload["format"]["duration"])
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid audio: missing duration/sample-rate/channel metadata") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid audio: duration must be positive")
    return AudioProbe(
        duration_seconds=duration,
        codec=str(stream.get("codec_name") or "unknown"),
        sample_rate=sample_rate,
        channels=channels,
    )


def probe_video(path: Path) -> VideoProbe:
    payload = _probe_json(path)
    video_streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        raise ValueError("video encode failed: no video stream")
    stream = video_streams[0]
    rate = str(stream.get("r_frame_rate") or "0/1")
    numerator, denominator = rate.split("/", maxsplit=1)
    fps = float(numerator) / float(denominator)
    duration = float(payload["format"]["duration"])
    return VideoProbe(
        duration_seconds=duration,
        width=int(stream["width"]),
        height=int(stream["height"]),
        fps=fps,
        has_audio=any(s.get("codec_type") == "audio" for s in payload.get("streams", [])),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_avatar_image(
    source: Path,
    destination: Path,
    *,
    requested_width: int,
    requested_height: int,
    pipeline_width: int,
    pipeline_height: int,
) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"avatar image input does not exist: {source}")
    try:
        with Image.open(source) as opened:
            image = opened.convert("RGB")
    except Exception as exc:
        raise ValueError(f"invalid image: failed to decode {source.name}") from exc

    fitted = ImageOps.fit(
        image,
        (requested_width, requested_height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )
    pad_top = max(0, (pipeline_height - requested_height) // 2)
    pad_bottom = max(0, pipeline_height - requested_height - pad_top)
    pad_left = max(0, (pipeline_width - requested_width) // 2)
    pad_right = max(0, pipeline_width - requested_width - pad_left)
    padded = ImageOps.expand(
        fitted,
        border=(pad_left, pad_top, pad_right, pad_bottom),
        fill=None,
    )
    if pad_top:
        top = fitted.crop((0, 0, requested_width, 1)).resize((requested_width, pad_top))
        padded.paste(top, (pad_left, 0))
    if pad_bottom:
        bottom = fitted.crop(
            (0, requested_height - 1, requested_width, requested_height)
        ).resize((requested_width, pad_bottom))
        padded.paste(bottom, (pad_left, pad_top + requested_height))
    if pad_left or pad_right:
        raise ValueError("A2V canonical grid adapter does not support horizontal padding")
    destination.parent.mkdir(parents=True, exist_ok=True)
    padded.save(destination, format="PNG")
    return destination


class DirectLTX25A2VBackend:
    """Resident official LTX-2.5 A2Vid two-stage adapter."""

    def __init__(self, *, model_root: Path, device: str = "cuda") -> None:
        self._model_files = LTXA2VModelFiles.from_root(model_root)
        self._device = device
        self._bindings: _A2VBindings | None = None
        self._pipeline: Any | None = None
        self._model_load_seconds = 0.0
        self._lock = threading.Lock()

    def prepare(self) -> None:
        """Validate A2V assets without duplicating the resident I2V model at startup."""
        with self._lock:
            bindings = self._get_bindings()
            try:
                self._validate_runtime(bindings)
            except FileNotFoundError as exc:
                raise ModelBootstrapPendingError(str(exc)) from exc

    def ready(self) -> None:
        """Keep readiness cheap; the A2V pipeline is lazy-loaded once and then reused."""
        with self._lock:
            bindings = self._get_bindings()
            self._validate_runtime(bindings)

    def generate(
        self,
        *,
        avatar_image_path: Path,
        audio_path: Path,
        output_path: Path,
        parameters: LTXA2VParameters,
    ) -> A2VGenerationResult:
        audio_probe = probe_audio(audio_path)
        with self._lock:
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            with self._inference_context(bindings):
                pipeline = self._get_or_build_pipeline(bindings)
                pipeline_width = _round_up_to_grid(parameters.width)
                pipeline_height = _round_up_to_grid(parameters.height)
                conditioning_path = _prepare_avatar_image(
                    avatar_image_path,
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
                guider = bindings.multi_modal_guider_params(
                    cfg_scale=3.0,
                    stg_scale=1.0,
                    rescale_scale=0.7,
                    modality_scale=3.0,
                    skip_step=0,
                    stg_blocks=[28],
                )
                inference_started = time.monotonic()
                result = pipeline(
                    prompt=parameters.prompt,
                    negative_prompt=bindings.default_negative_prompt,
                    seed=parameters.seed,
                    height=pipeline_height,
                    width=pipeline_width,
                    num_frames=None,
                    frame_rate=float(parameters.fps),
                    num_inference_steps=30,
                    video_guider_params=guider,
                    images=[conditioning],
                    audio_path=str(audio_path.resolve()),
                    audio_start_time=0.0,
                    audio_max_duration=None,
                    tiling_config=bindings.auto_tiling,
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

        output_probe = probe_video(output_path)
        if not output_probe.has_audio:
            raise ValueError("video encode failed: A2V output does not contain an audio stream")
        if (output_probe.width, output_probe.height) != (parameters.width, parameters.height):
            raise ValueError(
                "video encode failed: output resolution "
                f"{output_probe.width}x{output_probe.height} does not match "
                f"{parameters.width}x{parameters.height}"
            )
        effective = result.num_frames / float(parameters.fps)
        return A2VGenerationResult(
            num_frames=int(result.num_frames),
            effective_audio_duration_seconds=effective,
            output_video_duration_seconds=output_probe.duration_seconds,
            model_load_seconds=self._model_load_seconds,
            inference_seconds=inference_seconds,
            encode_seconds=encode_seconds,
            input_audio=audio_probe,
            output_video=output_probe,
        )

    def _get_bindings(self) -> _A2VBindings:
        if self._bindings is None:
            self._bindings = _load_a2v_bindings()
        return self._bindings

    def _validate_runtime(self, bindings: _A2VBindings) -> None:
        self._model_files.validate()
        if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the LTX-2.5 A2V runtime")

    @staticmethod
    def _inference_context(bindings: _A2VBindings) -> Any:
        inference_mode = getattr(bindings.torch, "inference_mode", None)
        return nullcontext() if inference_mode is None else inference_mode()

    def _get_or_build_pipeline(self, bindings: _A2VBindings) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        started = time.monotonic()
        model_paths = bindings.model_paths.from_split(
            transformer_path=str(self._model_files.transformer),
            text_encoder_path=str(self._model_files.text_encoder),
            video_vae_path=str(self._model_files.video_vae),
            audio_vae_path=str(self._model_files.audio_vae),
        )
        distilled_lora = [
            bindings.lora_path_strength_and_sd_ops(
                str(self._model_files.distilled_lora),
                1.0,
                bindings.lora_sd_ops,
            )
        ]
        quantization = bindings.quantization_kind.FP8_CAST.to_policy(
            checkpoint_path=str(self._model_files.transformer)
        )
        _force_diffvae_eager_sdpa(bindings.diffvae_apply)
        self._pipeline = bindings.pipeline_class(
            model_paths=model_paths,
            distilled_lora=distilled_lora,
            spatial_upsampler_path=str(self._model_files.spatial_upsampler),
            loras=[],
            device=bindings.torch.device(self._device),
            quantization=quantization,
            offload_mode=bindings.offload_mode.CPU,
        )
        self._model_load_seconds = time.monotonic() - started
        return self._pipeline


class LTXA2VTaskRunner:
    task_name = LTX_A2V_TASK

    def __init__(self, *, backend: LTXA2VBackend) -> None:
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
        parameters = LTXA2VParameters.model_validate(request.parameters)
        output = work_dir / "output.mp4"
        result = self._backend.generate(
            avatar_image_path=inputs["avatar_image"],
            audio_path=inputs["audio"],
            output_path=output,
            parameters=parameters,
        )
        metadata_path = work_dir / "metadata.json"
        metadata = {
            "schema_version": "1",
            "generation_mode": "audio_to_video",
            "generation_profile": parameters.generation_profile,
            "input_audio_duration_seconds": result.input_audio.duration_seconds,
            "effective_audio_duration_seconds": result.effective_audio_duration_seconds,
            "output_video_duration_seconds": result.output_video_duration_seconds,
            "duration_delta_seconds": (
                result.output_video_duration_seconds - result.input_audio.duration_seconds
            ),
            "fps": parameters.fps,
            "num_frames": result.num_frames,
            "width": parameters.width,
            "height": parameters.height,
            "seed": parameters.seed,
            "audio_codec": result.input_audio.codec,
            "audio_sample_rate": result.input_audio.sample_rate,
            "audio_channels": result.input_audio.channels,
            "image_sha256": _sha256(inputs["avatar_image"]),
            "audio_sha256": _sha256(inputs["audio"]),
            "quantization": "fp8-cast",
            "offload_mode": "cpu",
            "model_load_seconds": result.model_load_seconds,
            "inference_seconds": result.inference_seconds,
            "encode_seconds": result.encode_seconds,
            "recommended_max_duration_seconds": LTX_A2V_RECOMMENDED_MAX_SECONDS,
            "hard_max_duration_seconds": LTX_A2V_HARD_MAX_SECONDS,
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return LocalArtifact(
            path=output,
            content_type="video/mp4",
            sidecars={
                "metadata": LocalArtifactPart(
                    path=metadata_path,
                    content_type="application/json",
                )
            },
        )

    def _validate_request(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
    ) -> None:
        if request.task != self.task_name:
            raise ValueError(f"LTXA2VTaskRunner cannot execute task {request.task!r}")
        required_inputs = {"avatar_image", "audio"}
        if set(inputs) != required_inputs:
            raise ValueError(
                "video.ltx25.audio_to_video requires exactly 'avatar_image' and 'audio' inputs"
            )
        if {item.name for item in request.inputs} != required_inputs:
            raise ValueError("A2V request must declare 'avatar_image' and 'audio' inputs")
        if request.output.content_type != "video/mp4":
            raise ValueError("A2V output content_type must be 'video/mp4'")
        if Path(request.output.key).suffix.lower() != ".mp4":
            raise ValueError("A2V output key must end in .mp4")
        if not request.sidecar_outputs or set(request.sidecar_outputs) != {"metadata"}:
            raise ValueError("A2V request must declare exactly one 'metadata' sidecar output")
        metadata_output = request.sidecar_outputs["metadata"]
        if metadata_output.content_type != "application/json":
            raise ValueError("A2V metadata sidecar content_type must be 'application/json'")
        if Path(metadata_output.key).suffix.lower() != ".json":
            raise ValueError("A2V metadata sidecar key must end in .json")
