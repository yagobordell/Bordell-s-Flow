from __future__ import annotations

import json
import subprocess
import wave
from collections.abc import Callable
from math import floor, isclose
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ai_video_factory.domain import NarrationAudio

from .media import MediaProbe, probe_video
from .models import CompositionPlan

ProbeVideo = Callable[[Path], MediaProbe]


class WavProbe(BaseModel):
    """Measured properties of the canonical Phase 5 narration WAV."""

    model_config = ConfigDict(extra="forbid")

    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    sample_width_bytes: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)


class AudioStreamProbe(BaseModel):
    """Normalized metadata for the single audio stream in the final MP4."""

    model_config = ConfigDict(extra="forbid")

    codec_name: str = Field(min_length=1)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    duration_seconds: float | None = Field(default=None, gt=0)


class FinalMuxInputs(BaseModel):
    """Validated Phase 9.5 inputs before ffmpeg is invoked."""

    model_config = ConfigDict(extra="forbid")

    canonical_duration_seconds: float = Field(gt=0)
    video: MediaProbe
    narration: WavProbe


class FinalMuxResult(BaseModel):
    """Validated output metadata returned by the final mux operation."""

    model_config = ConfigDict(extra="forbid")

    canonical_duration_seconds: float = Field(gt=0)
    video: MediaProbe
    audio: AudioStreamProbe


def probe_wav(path: Path) -> WavProbe:
    """Measure the canonical narration WAV from the PCM frames actually present."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Narration WAV not found: {resolved}")

    try:
        with wave.open(str(resolved), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            declared_frame_count = wav_file.getnframes()

            if (
                sample_rate <= 0
                or channels <= 0
                or sample_width <= 0
                or declared_frame_count <= 0
            ):
                raise ValueError(f"Narration WAV has invalid stream properties: {resolved}")

            frame_data = wav_file.readframes(declared_frame_count)
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"Narration WAV is invalid: {resolved}") from exc

    frame_size = channels * sample_width
    if not frame_data or len(frame_data) % frame_size != 0:
        raise ValueError(f"Narration WAV contains incomplete PCM frames: {resolved}")

    actual_frame_count = len(frame_data) // frame_size
    return WavProbe(
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frame_count=actual_frame_count,
        duration_seconds=actual_frame_count / sample_rate,
    )


def probe_audio_stream(path: Path) -> AudioStreamProbe:
    """Inspect the first audio stream in a muxed media file with ffprobe."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Muxed media file not found: {resolved}")

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,duration",
                "-of",
                "json",
                str(resolved),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe executable was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or "unknown ffprobe error"
        raise RuntimeError(f"ffprobe failed for {resolved}: {details}") from exc

    try:
        payload: Any = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {resolved}") from exc

    return parse_audio_stream_payload(payload, source=resolved)


def parse_audio_stream_payload(
    payload: Any,
    *,
    source: Path | str = "media",
) -> AudioStreamProbe:
    """Normalize ffprobe JSON for one final audio stream."""

    if not isinstance(payload, dict):
        raise ValueError(f"ffprobe audio payload for {source} must be an object")
    streams = payload.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise ValueError(f"Expected exactly one selected audio stream for {source}")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise ValueError(f"Invalid audio stream payload for {source}")

    codec_name = stream.get("codec_name")
    if not isinstance(codec_name, str) or not codec_name:
        raise ValueError(f"Missing audio codec for {source}")

    sample_rate = _positive_int(stream.get("sample_rate"), "sample_rate", source)
    channels = _positive_int(stream.get("channels"), "channels", source)
    duration_seconds = _optional_positive_float(stream.get("duration"), source)
    return AudioStreamProbe(
        codec_name=codec_name,
        sample_rate=sample_rate,
        channels=channels,
        duration_seconds=duration_seconds,
    )


def validate_final_mux_inputs(
    plan: CompositionPlan,
    narration: NarrationAudio,
    visual_path: Path,
    audio_path: Path,
    *,
    probe_video_fn: ProbeVideo = probe_video,
) -> FinalMuxInputs:
    """Reject any input that would make the final mux rewrite canonical timing."""

    video = probe_video_fn(visual_path.resolve())
    canonical_duration = plan.total_frames / plan.fps
    frame_tolerance = 1 / plan.fps

    if video.codec_name != "h264":
        raise ValueError(f"Phase 9.5 visual input must be H.264, found {video.codec_name}")
    if (video.width, video.height) != (plan.width, plan.height):
        raise ValueError("Phase 9.5 visual dimensions do not match the composition plan")
    if not isclose(video.fps, float(plan.fps), rel_tol=0.0, abs_tol=1e-6):
        raise ValueError(f"Phase 9.5 visual input must be {plan.fps} fps")
    if video.audio_stream_count != 0:
        raise ValueError("Phase 9.5 visual input must remain silent before final muxing")
    if _frame_count(video, fps=plan.fps) != plan.total_frames:
        raise ValueError("Phase 9.5 visual frame count does not match the composition plan")
    if abs(video.duration_seconds - canonical_duration) > frame_tolerance:
        raise ValueError("Phase 9.5 visual duration differs from the canonical timeline")

    wav = probe_wav(audio_path)
    if Path(narration.uri).name != audio_path.name:
        raise ValueError("Narration metadata URI does not match the selected WAV file")

    if wav.duration_seconds > canonical_duration + 1e-9:
        raise ValueError(
            "Narration WAV exceeds the canonical composition; refusing to truncate audio: "
            f"wav={wav.duration_seconds:.6f}s canonical={canonical_duration:.6f}s"
        )
    if abs(wav.duration_seconds - canonical_duration) > frame_tolerance:
        raise ValueError(
            "Narration WAV duration differs from the canonical composition by over one frame: "
            f"wav={wav.duration_seconds:.6f}s canonical={canonical_duration:.6f}s"
        )
    if abs(wav.duration_seconds - narration.duration_seconds) > frame_tolerance:
        raise ValueError(
            "Narration WAV duration differs from NarrationAudio metadata by over one frame: "
            f"wav={wav.duration_seconds:.6f}s metadata={narration.duration_seconds:.6f}s"
        )

    return FinalMuxInputs(
        canonical_duration_seconds=canonical_duration,
        video=video,
        narration=wav,
    )


def build_final_mux_command(
    visual_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    duration_seconds: float,
    audio_bitrate_kbps: int = 192,
) -> list[str]:
    """Build the deterministic ffmpeg command used for Phase 9.5."""

    if duration_seconds <= 0:
        raise ValueError("Final mux duration must be positive")
    if not 32 <= audio_bitrate_kbps <= 512:
        raise ValueError("AAC bitrate must be between 32 and 512 kbps")

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(visual_path.resolve()),
        "-i",
        str(audio_path.resolve()),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_bitrate_kbps}k",
        "-t",
        f"{duration_seconds:.6f}",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        str(output_path.resolve()),
    ]


def mux_final_video(
    plan: CompositionPlan,
    narration: NarrationAudio,
    visual_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    audio_bitrate_kbps: int = 192,
) -> FinalMuxResult:
    """Mux the Phase 9.4 visual with canonical narration and validate the result."""

    inputs = validate_final_mux_inputs(plan, narration, visual_path, audio_path)
    output_path.resolve().parent.mkdir(parents=True, exist_ok=True)
    command = build_final_mux_command(
        visual_path,
        audio_path,
        output_path,
        duration_seconds=inputs.canonical_duration_seconds,
        audio_bitrate_kbps=audio_bitrate_kbps,
    )

    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg executable was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg final mux failed with exit code {exc.returncode}") from exc

    return validate_final_mux_output(plan, output_path)


def validate_final_mux_output(
    plan: CompositionPlan,
    output_path: Path,
    *,
    probe_video_fn: ProbeVideo = probe_video,
    probe_audio_fn: Callable[[Path], AudioStreamProbe] = probe_audio_stream,
) -> FinalMuxResult:
    """Validate the final MP4 while allowing normal AAC packet-duration rounding."""

    video = probe_video_fn(output_path.resolve())
    canonical_duration = plan.total_frames / plan.fps

    if video.codec_name != "h264":
        raise ValueError(f"Final video must use H.264, found {video.codec_name}")
    if (video.width, video.height) != (plan.width, plan.height):
        raise ValueError("Final video dimensions do not match the composition plan")
    if not isclose(video.fps, float(plan.fps), rel_tol=0.0, abs_tol=1e-6):
        raise ValueError(f"Final video must remain {plan.fps} fps")
    if _frame_count(video, fps=plan.fps) != plan.total_frames:
        raise ValueError("Final video frame count does not match the canonical timeline")
    if video.audio_stream_count != 1:
        raise ValueError("Final video must contain exactly one audio stream")
    if abs(video.duration_seconds - canonical_duration) > 1 / plan.fps:
        raise ValueError("Final video duration differs from the canonical timeline")

    audio = probe_audio_fn(output_path.resolve())
    if audio.codec_name != "aac":
        raise ValueError(f"Final audio must use AAC, found {audio.codec_name}")
    if audio.duration_seconds is not None:
        if abs(audio.duration_seconds - canonical_duration) > 2 / plan.fps:
            raise ValueError(
                "Final AAC stream duration differs too much from the canonical timeline"
            )

    return FinalMuxResult(
        canonical_duration_seconds=canonical_duration,
        video=video,
        audio=audio,
    )


def _frame_count(media: MediaProbe, *, fps: int) -> int:
    if media.frame_count is not None:
        return media.frame_count
    return floor(media.duration_seconds * fps + 0.5)


def _positive_int(raw: Any, label: str, source: Path | str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"Invalid {label} for {source}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Missing or invalid {label} for {source}") from exc
    if value <= 0:
        raise ValueError(f"{label} for {source} must be positive")
    return value


def _optional_positive_float(raw: Any, source: Path | str) -> float | None:
    if raw in {None, "", "N/A"}:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid audio duration for {source}: {raw}") from exc
    if value <= 0:
        raise ValueError(f"Audio duration for {source} must be positive")
    return value
