from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MediaProbe(BaseModel):
    """Normalized ffprobe metadata required by the compositor."""

    model_config = ConfigDict(extra="forbid")

    codec_name: str = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    frame_count: int | None = Field(default=None, gt=0)
    audio_stream_count: int = Field(ge=0)


def probe_video(path: Path) -> MediaProbe:
    """Inspect one local video file with ffprobe and normalize the relevant metadata."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Video clip file not found: {resolved}")

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
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

    return parse_ffprobe_payload(payload, source=resolved)


def parse_ffprobe_payload(payload: Any, *, source: Path | str = "video") -> MediaProbe:
    """Normalize ffprobe JSON. Kept public so parsing can be tested without ffprobe installed."""

    if not isinstance(payload, dict):
        raise ValueError(f"ffprobe payload for {source} must be an object")

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ValueError(f"ffprobe payload for {source} is missing streams")

    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if len(video_streams) != 1:
        raise ValueError(f"Expected exactly one video stream for {source}")

    video = video_streams[0]
    codec_name = _required_string(video, "codec_name", source)
    width = _required_positive_int(video, "width", source)
    height = _required_positive_int(video, "height", source)
    fps = _frame_rate(video, source)
    frame_count = _optional_positive_int(video.get("nb_frames"), source)
    duration_seconds = _duration_seconds(video, payload.get("format"), source)

    return MediaProbe(
        codec_name=codec_name,
        width=width,
        height=height,
        fps=fps,
        duration_seconds=duration_seconds,
        frame_count=frame_count,
        audio_stream_count=len(audio_streams),
    )


def _frame_rate(video: dict[str, Any], source: Path | str) -> float:
    raw = video.get("avg_frame_rate") or video.get("r_frame_rate")
    if not isinstance(raw, str) or raw in {"0/0", "N/A", ""}:
        raise ValueError(f"Video stream for {source} has no usable frame rate")
    try:
        value = float(Fraction(raw))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Invalid frame rate for {source}: {raw}") from exc
    if value <= 0:
        raise ValueError(f"Frame rate for {source} must be positive")
    return value


def _duration_seconds(
    video: dict[str, Any],
    raw_format: Any,
    source: Path | str,
) -> float:
    raw = video.get("duration")
    if raw in {None, "N/A", ""} and isinstance(raw_format, dict):
        raw = raw_format.get("duration")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Video stream for {source} has no usable duration") from exc
    if value <= 0:
        raise ValueError(f"Duration for {source} must be positive")
    return value


def _required_string(
    value: dict[str, Any],
    key: str,
    source: Path | str,
) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"Missing {key} in video stream for {source}")
    return raw


def _required_positive_int(
    value: dict[str, Any],
    key: str,
    source: Path | str,
) -> int:
    raw = value.get(key)
    if isinstance(raw, bool):
        raise ValueError(f"Invalid {key} in video stream for {source}")
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Missing or invalid {key} in video stream for {source}") from exc
    if parsed <= 0:
        raise ValueError(f"{key} in video stream for {source} must be positive")
    return parsed


def _optional_positive_int(raw: Any, source: Path | str) -> int | None:
    if raw in {None, "N/A", ""}:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"Invalid frame count for {source}")
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid frame count for {source}: {raw}") from exc
    if parsed <= 0:
        raise ValueError(f"Frame count for {source} must be positive")
    return parsed
