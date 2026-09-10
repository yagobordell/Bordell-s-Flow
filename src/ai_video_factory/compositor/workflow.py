from __future__ import annotations

from collections.abc import Callable
from math import floor, isclose
from pathlib import Path

from ai_video_factory.domain import NarrationWord, ShotTiming, VideoClip

from .captions import build_caption_cues
from .media import MediaProbe, probe_video
from .models import CompositionPlan, CompositionShot
from .timeline import quantize_shot_timings

ProbeVideo = Callable[[Path], MediaProbe]


def build_composition_plan(
    clips: list[VideoClip],
    timings: list[ShotTiming],
    *,
    clip_base_dir: Path,
    words: list[NarrationWord] | None = None,
    width: int = 768,
    height: int = 1280,
    fps: int = 24,
    caption_max_words: int = 5,
    caption_max_chars: int = 36,
    caption_max_duration_seconds: float = 2.5,
    caption_pause_threshold_seconds: float = 0.35,
    probe: ProbeVideo = probe_video,
) -> CompositionPlan:
    """Build and validate the frame-exact Phase 9 visual and caption timeline."""

    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("Composition dimensions and fps must be positive")
    _validate_clip_ids(clips, timings)

    intervals = quantize_shot_timings(timings, fps=fps)
    base_dir = clip_base_dir.resolve()
    shots: list[CompositionShot] = []

    for clip, interval in zip(clips, intervals, strict=True):
        clip_path = (base_dir / clip.uri).resolve()
        if not clip_path.is_relative_to(base_dir):
            raise ValueError(f"Shot {clip.shot_id} video URI escapes its base directory")
        if not clip_path.is_file():
            raise FileNotFoundError(f"Video clip file not found: {clip_path}")

        media = probe(clip_path)
        _validate_media(
            clip.shot_id,
            media,
            required_frames=interval.duration_frames,
            width=width,
            height=height,
            fps=fps,
        )
        shots.append(
            CompositionShot(
                shot_id=clip.shot_id,
                uri=clip_path.as_posix(),
                start_frame=interval.start_frame,
                end_frame=interval.end_frame,
                duration_frames=interval.duration_frames,
                source_duration_seconds=media.duration_seconds,
                source_frame_count=media.frame_count,
            )
        )

    total_frames = shots[-1].end_frame
    captions = []
    if words is not None:
        captions = build_caption_cues(
            words,
            fps=fps,
            total_frames=total_frames,
            max_words=caption_max_words,
            max_chars=caption_max_chars,
            max_duration_seconds=caption_max_duration_seconds,
            pause_threshold_seconds=caption_pause_threshold_seconds,
        )

    return CompositionPlan(
        width=width,
        height=height,
        fps=fps,
        total_frames=total_frames,
        shots=shots,
        captions=captions,
    )


def _validate_clip_ids(clips: list[VideoClip], timings: list[ShotTiming]) -> None:
    if not clips:
        raise ValueError("Composition requires at least one video clip")

    clip_ids = [clip.shot_id for clip in clips]
    timing_ids = [timing.shot_id for timing in timings]
    expected = list(range(1, len(clips) + 1))

    if clip_ids != expected:
        raise ValueError("Video clips must have consecutive shot IDs starting at 1")
    if timing_ids != clip_ids:
        raise ValueError("Video clips and shot timings must have identical ordered shot IDs")


def _validate_media(
    shot_id: int,
    media: MediaProbe,
    *,
    required_frames: int,
    width: int,
    height: int,
    fps: int,
) -> None:
    if media.codec_name != "h264":
        raise ValueError(
            f"Shot {shot_id} must use H.264 video, found codec={media.codec_name}"
        )
    if (media.width, media.height) != (width, height):
        raise ValueError(
            f"Shot {shot_id} dimensions must be {width}x{height}, "
            f"found {media.width}x{media.height}"
        )
    if not isclose(media.fps, float(fps), rel_tol=0.0, abs_tol=1e-6):
        raise ValueError(f"Shot {shot_id} must be {fps} fps, found {media.fps:g}")
    if media.audio_stream_count != 0:
        raise ValueError(f"Shot {shot_id} Phase 8 source clip must not contain audio")

    available_frames = media.frame_count
    if available_frames is None:
        available_frames = floor(media.duration_seconds * fps + 0.5)
    if available_frames < required_frames:
        raise ValueError(
            f"Shot {shot_id} source clip is too short: "
            f"required_frames={required_frames}, available_frames={available_frames}"
        )
