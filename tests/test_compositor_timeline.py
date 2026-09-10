import pytest

from ai_video_factory.compositor.timeline import quantize_shot_timings
from ai_video_factory.domain import ShotTiming


def test_quantizes_absolute_boundaries_without_accumulating_duration_rounding() -> None:
    timings = [
        ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=0.0625),
        ShotTiming(shot_id=2, start_seconds=0.0625, end_seconds=0.125),
    ]

    intervals = quantize_shot_timings(timings, fps=24)

    assert [(item.start_frame, item.end_frame) for item in intervals] == [(0, 2), (2, 3)]
    assert [item.duration_frames for item in intervals] == [2, 1]


def test_rejects_non_contiguous_shot_timings() -> None:
    timings = [
        ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=1.0),
        ShotTiming(shot_id=2, start_seconds=1.1, end_seconds=2.0),
    ]

    with pytest.raises(ValueError, match="contiguous"):
        quantize_shot_timings(timings, fps=24)


def test_rejects_timing_that_collapses_to_zero_frames() -> None:
    timings = [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=0.01)]

    with pytest.raises(ValueError, match="non-positive frame interval"):
        quantize_shot_timings(timings, fps=24)
