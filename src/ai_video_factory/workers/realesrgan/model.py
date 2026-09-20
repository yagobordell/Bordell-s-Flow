from __future__ import annotations

import math
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_video_factory.compositor.media import MediaProbe, probe_video
from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact

REALESRGAN_TASK = "video.realesrgan.upscale"
REALESRGAN_MODEL_NAME = "RealESRGAN_x2plus"
REALESRGAN_GENERATION_PROFILE = "realesrgan-x2plus-native2x-h264-crf12-v1"
REALESRGAN_MODEL_SHA256 = "49fafd45f8fd7aa8d31ab2a22d14d91b536c34494a5cfe31eb5d89c2fa266abb"
_MODEL_FILENAME = "RealESRGAN_x2plus.pth"


class RealESRGANParameters(BaseModel):
    """All controls that can change the pixels of one upscaled clip."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_name: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_width: int = Field(gt=0)
    source_height: int = Field(gt=0)
    source_frame_count: int = Field(gt=0)
    target_width: int = Field(gt=0)
    target_height: int = Field(gt=0)
    fps: int = Field(gt=0, le=120)
    tile: int = Field(default=0, ge=0)
    tile_pad: int = Field(default=10, ge=0, le=128)
    pre_pad: int = Field(default=0, ge=0, le=128)
    fp32: bool = False
    encoder: str = "libx264"
    crf: int = Field(default=12, ge=0, le=51)
    preset: str = "medium"
    pixel_format: str = "yuv420p"

    @field_validator("generation_profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        if value != REALESRGAN_GENERATION_PROFILE:
            raise ValueError(
                f"generation_profile must be exactly {REALESRGAN_GENERATION_PROFILE!r}"
            )
        return value

    @field_validator("model_name")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        if value != REALESRGAN_MODEL_NAME:
            raise ValueError(f"model_name must be exactly {REALESRGAN_MODEL_NAME!r}")
        return value

    @model_validator(mode="after")
    def validate_exact_two_x(self) -> Self:
        if self.target_width != self.source_width * 2:
            raise ValueError("Real-ESRGAN target width must be exactly 2x source width")
        if self.target_height != self.source_height * 2:
            raise ValueError("Real-ESRGAN target height must be exactly 2x source height")
        if self.encoder != "libx264":
            raise ValueError("Real-ESRGAN intermediate encoder must be libx264")
        if self.preset not in {"slow", "medium", "fast"}:
            raise ValueError("Real-ESRGAN preset must be slow, medium or fast")
        if self.pixel_format != "yuv420p":
            raise ValueError("Real-ESRGAN intermediate pixel format must be yuv420p")
        return self


class RealESRGANBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def upscale(
        self,
        *,
        source_path: Path,
        output_path: Path,
        parameters: RealESRGANParameters,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _Bindings:
    numpy: Any
    torch: Any
    rrdbnet_type: Any
    upsampler_type: Any


def _load_bindings() -> _Bindings:
    try:
        import numpy as np
        import torch
        from basicsr.archs.rrdbnet_arch import RRDBNet
        from realesrgan import RealESRGANer
    except ImportError as exc:
        raise RuntimeError("Real-ESRGAN runtime dependencies are not installed") from exc
    return _Bindings(
        numpy=np,
        torch=torch,
        rrdbnet_type=RRDBNet,
        upsampler_type=RealESRGANer,
    )


class DirectRealESRGANBackend:
    """Resident RealESRGAN_x2plus runtime with frame-preserving FFmpeg pipes."""

    def __init__(self, *, model_root: Path, device: str = "cuda") -> None:
        self._model_root = model_root
        self._device = device
        self._bindings: _Bindings | None = None
        self._upsampler: Any | None = None
        self._upsampler_signature: tuple[int, int, int, bool] | None = None
        self._lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        return self._model_root / _MODEL_FILENAME

    @property
    def bootstrap_marker(self) -> Path:
        return self._model_root / ".ready"

    def prepare(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            self._get_or_build_upsampler()

    def ready(self) -> None:
        # Readiness must remain responsive while a multi-minute video upscale owns
        # the inference lock. Preparation completes before queue traffic is enabled,
        # and inference never sets the resident upsampler back to None.
        self._validate_bootstrap()
        if self._upsampler is None:
            raise RuntimeError("Real-ESRGAN runtime has not been prepared")

    def upscale(
        self,
        *,
        source_path: Path,
        output_path: Path,
        parameters: RealESRGANParameters,
    ) -> None:
        with self._lock:
            self._validate_source(source_path, parameters)
            bindings = self._get_bindings()
            upsampler = self._get_or_build_upsampler(
                tile=parameters.tile,
                tile_pad=parameters.tile_pad,
                pre_pad=parameters.pre_pad,
                fp32=parameters.fp32,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.unlink(missing_ok=True)

            frame_bytes = parameters.source_width * parameters.source_height * 3
            decode = subprocess.Popen(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(source_path),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-sn",
                    "-dn",
                    "-vsync",
                    "0",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            encode = subprocess.Popen(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "bgr24",
                    "-s:v",
                    f"{parameters.target_width}x{parameters.target_height}",
                    "-r",
                    str(parameters.fps),
                    "-i",
                    "pipe:0",
                    "-an",
                    "-c:v",
                    parameters.encoder,
                    "-preset",
                    parameters.preset,
                    "-crf",
                    str(parameters.crf),
                    "-pix_fmt",
                    parameters.pixel_format,
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if decode.stdout is None or encode.stdin is None:
                raise RuntimeError("FFmpeg frame pipes were not created")

            frame_count = 0
            started = time.monotonic()
            self._log_progress(
                bindings,
                event="start",
                frame_count=0,
                total_frames=parameters.source_frame_count,
                started=started,
            )
            try:
                while True:
                    raw = decode.stdout.read(frame_bytes)
                    if not raw:
                        break
                    if len(raw) != frame_bytes:
                        raise RuntimeError("FFmpeg decoder returned a partial raw video frame")
                    rgb = bindings.numpy.frombuffer(raw, dtype=bindings.numpy.uint8).reshape(
                        parameters.source_height,
                        parameters.source_width,
                        3,
                    )
                    bgr = bindings.numpy.ascontiguousarray(rgb[:, :, ::-1])
                    enhanced, _ = upsampler.enhance(bgr, outscale=2)
                    if enhanced.shape[:2] != (
                        parameters.target_height,
                        parameters.target_width,
                    ):
                        raise RuntimeError(
                            "Real-ESRGAN returned unexpected frame dimensions: "
                            f"{enhanced.shape[1]}x{enhanced.shape[0]}"
                        )
                    encode.stdin.write(enhanced.tobytes())
                    frame_count += 1
                    if (
                        frame_count == 1
                        or frame_count % 10 == 0
                        or frame_count == parameters.source_frame_count
                    ):
                        self._log_progress(
                            bindings,
                            event="frame",
                            frame_count=frame_count,
                            total_frames=parameters.source_frame_count,
                            started=started,
                        )
            finally:
                decode.stdout.close()
                encode.stdin.close()

            decode_stderr = (decode.stderr.read() if decode.stderr is not None else b"").decode(
                "utf-8", errors="replace"
            )
            encode_stderr = (encode.stderr.read() if encode.stderr is not None else b"").decode(
                "utf-8", errors="replace"
            )
            decode_status = decode.wait()
            encode_status = encode.wait()
            if decode_status != 0:
                raise RuntimeError(f"FFmpeg decode failed: {decode_stderr.strip()}")
            if encode_status != 0:
                raise RuntimeError(f"FFmpeg encode failed: {encode_stderr.strip()}")
            if frame_count != parameters.source_frame_count:
                raise RuntimeError(
                    "Real-ESRGAN changed source frame count: "
                    f"expected={parameters.source_frame_count} observed={frame_count}"
                )

            output_probe = probe_video(output_path)
            self._validate_output(output_probe, parameters)
            elapsed = time.monotonic() - started
            fps_effective = frame_count / elapsed if elapsed > 0 else math.inf
            self._log_progress(
                bindings,
                event="complete",
                frame_count=frame_count,
                total_frames=parameters.source_frame_count,
                started=started,
            )
            print(
                "REALESRGAN_INFERENCE_METRIC "
                f"elapsed_seconds={elapsed:.3f} frames={frame_count} "
                f"frames_per_second={fps_effective:.3f} "
                f"source={parameters.source_width}x{parameters.source_height} "
                f"target={parameters.target_width}x{parameters.target_height} "
                f"model={parameters.model_name}",
                flush=True,
            )
            self._release_cuda_cache(bindings)

    def _log_progress(
        self,
        bindings: _Bindings,
        *,
        event: str,
        frame_count: int,
        total_frames: int,
        started: float,
    ) -> None:
        elapsed = time.monotonic() - started
        allocated = 0
        reserved = 0
        free = 0
        total = 0
        if self._device.startswith("cuda") and bindings.torch.cuda.is_available():
            allocated = int(bindings.torch.cuda.memory_allocated())
            reserved = int(bindings.torch.cuda.memory_reserved())
            try:
                free_value, total_value = bindings.torch.cuda.mem_get_info()
                free = int(free_value)
                total = int(total_value)
            except Exception:
                pass
        print(
            "REALESRGAN_PROGRESS "
            f"event={event} frame={frame_count}/{total_frames} "
            f"elapsed_seconds={elapsed:.3f} "
            f"cuda_allocated_bytes={allocated} "
            f"cuda_reserved_bytes={reserved} "
            f"cuda_free_bytes={free} cuda_total_bytes={total}",
            flush=True,
        )

    def _release_cuda_cache(self, bindings: _Bindings) -> None:
        if not self._device.startswith("cuda") or not bindings.torch.cuda.is_available():
            return
        try:
            bindings.torch.cuda.synchronize()
        finally:
            bindings.torch.cuda.empty_cache()

    def _get_bindings(self) -> _Bindings:
        if self._bindings is None:
            self._bindings = _load_bindings()
        return self._bindings

    def _get_or_build_upsampler(
        self,
        *,
        tile: int = 0,
        tile_pad: int = 10,
        pre_pad: int = 0,
        fp32: bool = False,
    ) -> Any:
        signature = (tile, tile_pad, pre_pad, fp32)
        if self._upsampler is not None and self._upsampler_signature == signature:
            return self._upsampler
        bindings = self._get_bindings()
        if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the Real-ESRGAN runtime")
        model = bindings.rrdbnet_type(
            num_in_ch=3,
            num_out_ch=3,
            num_feat=64,
            num_block=23,
            num_grow_ch=32,
            scale=2,
        )
        gpu_id = 0 if self._device.startswith("cuda") else None
        self._upsampler = bindings.upsampler_type(
            scale=2,
            model_path=str(self.model_path),
            model=model,
            tile=tile,
            tile_pad=tile_pad,
            pre_pad=pre_pad,
            half=not fp32,
            gpu_id=gpu_id,
        )
        self._upsampler_signature = signature
        return self._upsampler

    def _validate_bootstrap(self) -> None:
        expected = f"{REALESRGAN_MODEL_NAME}@{REALESRGAN_MODEL_SHA256}"
        if not self.model_path.is_file() or not self.bootstrap_marker.is_file():
            raise ModelBootstrapPendingError("Real-ESRGAN model weights are not ready")
        marker = self.bootstrap_marker.read_text(encoding="utf-8").strip()
        if marker != expected:
            raise ModelBootstrapPendingError("Real-ESRGAN bootstrap marker does not match weights")

    @staticmethod
    def _validate_source(path: Path, parameters: RealESRGANParameters) -> None:
        media = probe_video(path)
        if media.codec_name != "h264":
            raise ValueError(f"Real-ESRGAN source must be H.264, found {media.codec_name}")
        if (media.width, media.height) != (
            parameters.source_width,
            parameters.source_height,
        ):
            raise ValueError("Real-ESRGAN source dimensions do not match request")
        if not math.isclose(media.fps, parameters.fps, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("Real-ESRGAN source fps does not match request")
        if media.audio_stream_count:
            raise ValueError("Real-ESRGAN source clip must not contain audio")
        if media.frame_count is not None and media.frame_count != parameters.source_frame_count:
            raise ValueError("Real-ESRGAN source frame count does not match request")

    @staticmethod
    def _validate_output(media: MediaProbe, parameters: RealESRGANParameters) -> None:
        if media.codec_name != "h264":
            raise RuntimeError(f"Real-ESRGAN output must be H.264, found {media.codec_name}")
        if (media.width, media.height) != (
            parameters.target_width,
            parameters.target_height,
        ):
            raise RuntimeError("Real-ESRGAN output dimensions do not match request")
        if not math.isclose(media.fps, parameters.fps, rel_tol=0.0, abs_tol=1e-6):
            raise RuntimeError("Real-ESRGAN output fps does not match source")
        if media.audio_stream_count:
            raise RuntimeError("Real-ESRGAN output must not contain audio")
        if media.frame_count is not None and media.frame_count != parameters.source_frame_count:
            raise RuntimeError("Real-ESRGAN output frame count does not match source")


class RealESRGANVideoTaskRunner:
    task_name = REALESRGAN_TASK

    def __init__(self, *, backend: RealESRGANBackend) -> None:
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
        if request.task != self.task_name:
            raise ValueError(f"RealESRGANVideoTaskRunner cannot execute {request.task!r}")
        if set(inputs) != {"video"}:
            raise ValueError("video.realesrgan.upscale requires exactly one 'video' input")
        declared = {item.name for item in request.inputs}
        if declared != {"video"}:
            raise ValueError("Real-ESRGAN request must declare exactly one 'video' input")
        if request.output.content_type != "video/mp4":
            raise ValueError("Real-ESRGAN output content type must be video/mp4")
        if Path(request.output.key).suffix.lower() != ".mp4":
            raise ValueError("Real-ESRGAN output key must end in .mp4")

        parameters = RealESRGANParameters.model_validate(request.parameters)
        video_input = request.inputs[0]
        if video_input.sha256 != parameters.source_sha256:
            raise ValueError("Real-ESRGAN source SHA parameter must match object input SHA")

        output = work_dir / "upscaled.mp4"
        self._backend.upscale(
            source_path=inputs["video"],
            output_path=output,
            parameters=parameters,
        )
        if not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError("Real-ESRGAN backend produced no usable MP4 output")
        return LocalArtifact(path=output, content_type="video/mp4")
