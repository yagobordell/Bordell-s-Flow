from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from math import isclose

from ai_video_factory.domain import ShotTiming

_TIME_TOLERANCE_SECONDS = 1e-6


@dataclass(frozen=True, slots=True)
class FrameInterval:
    shot_id: int
    start_frame: int
    end_frame: int

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame


def quantize_shot_timings(
    timings: list[ShotTiming],
    *,
    fps: int = 24,
) -> list[FrameInterval]:
    """Quantize absolute canonical timing boundaries into contiguous frame intervals."""

    _validate_timings(timings)
    if fps <= 0:
        raise ValueError("Composition fps must be positive")

    intervals: list[FrameInterval] = []
    for index, timing in enumerate(timings):
        start_frame = seconds_to_frame(timing.start_seconds, fps)
        nearest_end_frame = seconds_to_frame(timing.end_seconds, fps)
        if nearest_end_frame <= start_frame:
            raise ValueError(
                "Shot timing collapses to a non-positive frame interval: "
                f"shot_id={timing.shot_id}, start_frame={start_frame}, "
                f"end_frame={nearest_end_frame}"
            )
        # The final boundary is the end of the measured narration.  Rounding it
        # down would make the visual timeline shorter than the WAV and would let
        # the final mux cut the last audio packet.  Earlier boundaries retain
        # nearest-frame quantization so shot boundaries do not drift.
        end_frame = (
            seconds_to_end_frame(timing.end_seconds, fps)
            if index == len(timings) - 1
            else nearest_end_frame
        )
        if end_frame <= start_frame:
            raise ValueError(
                "Shot timing collapses to a non-positive frame interval: "
                f"shot_id={timing.shot_id}, start_frame={start_frame}, end_frame={end_frame}"
            )
        intervals.append(
            FrameInterval(
                shot_id=timing.shot_id,
                start_frame=start_frame,
                end_frame=end_frame,
            )
        )

    for previous, current in zip(intervals, intervals[1:], strict=False):
        if previous.end_frame != current.start_frame:
            raise RuntimeError("Absolute timing quantization produced a non-contiguous timeline")

    return intervals


def seconds_to_frame(seconds: float, fps: int) -> int:
    """Map an absolute timestamp to a frame boundary using decimal round-half-up."""

    if seconds < 0:
        raise ValueError("Frame timestamp must be >= 0")
    if fps <= 0:
        raise ValueError("Composition fps must be positive")
    frames = Decimal(str(seconds)) * Decimal(fps)
    return int(frames.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def seconds_to_end_frame(seconds: float, fps: int) -> int:
    """Map the final absolute timestamp up to a frame boundary.

    The canonical visual timeline must contain the complete measured narration;
    a final frame of visual hold is preferable to truncating spoken audio.
    """

    if seconds < 0:
        raise ValueError("Frame timestamp must be >= 0")
    if fps <= 0:
        raise ValueError("Composition fps must be positive")
    frames = Decimal(str(seconds)) * Decimal(fps)
    return int(frames.quantize(Decimal("1"), rounding=ROUND_CEILING))


def _validate_timings(timings: list[ShotTiming]) -> None:
    if not timings:
        raise ValueError("Composition requires at least one shot timing")

    shot_ids = [timing.shot_id for timing in timings]
    if shot_ids != list(range(1, len(timings) + 1)):
        raise ValueError("Shot timings must have consecutive shot IDs starting at 1")

    if not isclose(timings[0].start_seconds, 0.0, abs_tol=_TIME_TOLERANCE_SECONDS):
        raise ValueError("Shot timings must start at 0.0 seconds")

    for timing in timings:
        if timing.end_seconds <= timing.start_seconds:
            raise ValueError(f"Shot {timing.shot_id} timing interval must be positive")

    for previous, current in zip(timings, timings[1:], strict=False):
        if not isclose(
            previous.end_seconds,
            current.start_seconds,
            abs_tol=_TIME_TOLERANCE_SECONDS,
        ):
            raise ValueError("Shot timings must be contiguous without gaps or overlaps")
