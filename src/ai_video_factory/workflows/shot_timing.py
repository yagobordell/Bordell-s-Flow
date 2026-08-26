from math import isclose

from ai_video_factory.domain import BeatTiming, Shot, ShotTiming

_TIME_TOLERANCE_SECONDS = 1e-6


def build_shot_timings(
    shots: list[Shot],
    beat_timings: list[BeatTiming],
) -> list[ShotTiming]:
    """Derive contiguous shot intervals from already-timed consecutive beats."""

    _validate_beat_timings(beat_timings)
    _validate_shots(shots, beat_timings)

    timing_by_beat_id = {timing.beat_id: timing for timing in beat_timings}
    shot_timings: list[ShotTiming] = []

    for shot in shots:
        first = timing_by_beat_id[shot.beat_ids[0]]
        last = timing_by_beat_id[shot.beat_ids[-1]]
        if last.end_seconds <= first.start_seconds:
            raise ValueError(
                "Shot timing produced a non-positive interval: "
                f"shot_id={shot.id}, start={first.start_seconds}, end={last.end_seconds}"
            )

        shot_timings.append(
            ShotTiming(
                shot_id=shot.id,
                start_seconds=first.start_seconds,
                end_seconds=last.end_seconds,
            )
        )

    for previous, current in zip(shot_timings, shot_timings[1:], strict=False):
        if not isclose(
            previous.end_seconds,
            current.start_seconds,
            abs_tol=_TIME_TOLERANCE_SECONDS,
        ):
            raise ValueError("Shot timings must be contiguous without gaps or overlaps")

    return shot_timings


def _validate_beat_timings(beat_timings: list[BeatTiming]) -> None:
    if not beat_timings:
        raise ValueError("Shot timing requires at least one beat timing")

    beat_ids = [timing.beat_id for timing in beat_timings]
    if beat_ids != list(range(1, len(beat_timings) + 1)):
        raise ValueError("Beat timings must have consecutive beat IDs starting at 1")

    if not isclose(
        beat_timings[0].start_seconds,
        0.0,
        abs_tol=_TIME_TOLERANCE_SECONDS,
    ):
        raise ValueError("Beat timings must start at 0.0 seconds")

    for timing in beat_timings:
        if timing.end_seconds <= timing.start_seconds:
            raise ValueError("Beat timing intervals must be positive")

    for previous, current in zip(beat_timings, beat_timings[1:], strict=False):
        if not isclose(
            previous.end_seconds,
            current.start_seconds,
            abs_tol=_TIME_TOLERANCE_SECONDS,
        ):
            raise ValueError("Beat timings must be contiguous without gaps or overlaps")


def _validate_shots(shots: list[Shot], beat_timings: list[BeatTiming]) -> None:
    if not shots:
        raise ValueError("Shot timing requires at least one shot")

    shot_ids = [shot.id for shot in shots]
    if shot_ids != list(range(1, len(shots) + 1)):
        raise ValueError("Shots must have consecutive IDs starting at 1")

    for shot in shots:
        if shot.beat_ids != list(range(shot.beat_ids[0], shot.beat_ids[-1] + 1)):
            raise ValueError(f"Shot {shot.id} must contain consecutive beat IDs")

    expected_beat_ids = [timing.beat_id for timing in beat_timings]
    returned_beat_ids = [beat_id for shot in shots for beat_id in shot.beat_ids]
    if returned_beat_ids != expected_beat_ids:
        raise ValueError("Shots must cover every timed beat exactly once and preserve order")
