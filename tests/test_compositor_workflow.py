from pathlib import Path

import pytest

from ai_video_factory.compositor import MediaProbe, build_composition_plan
from ai_video_factory.domain import ShotTiming, VideoClip


def _probe(
    *,
    duration_seconds: float = 1.0,
    frame_count: int | None = 24,
    codec_name: str = "h264",
    width: int = 768,
    height: int = 1280,
    fps: float = 24.0,
    audio_stream_count: int = 0,
):
    def fake_probe(_: Path) -> MediaProbe:
        return MediaProbe(
            codec_name=codec_name,
            width=width,
            height=height,
            fps=fps,
            duration_seconds=duration_seconds,
            frame_count=frame_count,
            audio_stream_count=audio_stream_count,
        )

    return fake_probe


def test_builds_frame_exact_plan_from_phase8_clips(tmp_path: Path) -> None:
    clips_dir = tmp_path / "phase8"
    media_dir = clips_dir / "video_clips"
    media_dir.mkdir(parents=True)
    for shot_id in (1, 2):
        (media_dir / f"shot_{shot_id:03d}.mp4").touch()

    clips = [
        VideoClip(shot_id=1, uri="video_clips/shot_001.mp4"),
        VideoClip(shot_id=2, uri="video_clips/shot_002.mp4"),
    ]
    timings = [
        ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=0.0625),
        ShotTiming(shot_id=2, start_seconds=0.0625, end_seconds=0.125),
    ]

    plan = build_composition_plan(
        clips,
        timings,
        clip_base_dir=clips_dir,
        probe=_probe(),
    )

    assert plan.total_frames == 3
    assert [(shot.start_frame, shot.end_frame) for shot in plan.shots] == [(0, 2), (2, 3)]
    assert [shot.duration_frames for shot in plan.shots] == [2, 1]
    assert plan.shots[0].uri == (media_dir / "shot_001.mp4").resolve().as_posix()


def test_rejects_source_clip_that_is_shorter_than_required(tmp_path: Path) -> None:
    clip_path = tmp_path / "shot_001.mp4"
    clip_path.touch()
    clips = [VideoClip(shot_id=1, uri="shot_001.mp4")]
    timings = [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=1.0)]

    with pytest.raises(ValueError, match="too short"):
        build_composition_plan(
            clips,
            timings,
            clip_base_dir=tmp_path,
            probe=_probe(duration_seconds=0.5, frame_count=12),
        )


def test_rejects_phase8_clip_with_audio(tmp_path: Path) -> None:
    clip_path = tmp_path / "shot_001.mp4"
    clip_path.touch()
    clips = [VideoClip(shot_id=1, uri="shot_001.mp4")]
    timings = [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=1.0)]

    with pytest.raises(ValueError, match="must not contain audio"):
        build_composition_plan(
            clips,
            timings,
            clip_base_dir=tmp_path,
            probe=_probe(audio_stream_count=1),
        )


def test_rejects_mismatched_clip_and_timing_ids(tmp_path: Path) -> None:
    clips = [VideoClip(shot_id=1, uri="shot_001.mp4")]
    timings = [ShotTiming(shot_id=2, start_seconds=0.0, end_seconds=1.0)]

    with pytest.raises(ValueError, match="identical ordered shot IDs"):
        build_composition_plan(clips, timings, clip_base_dir=tmp_path, probe=_probe())
