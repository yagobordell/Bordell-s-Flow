import pytest

from ai_video_factory.domain import BeatTiming, Shot
from ai_video_factory.workflows.shot_timing import build_shot_timings


def _beat_timings() -> list[BeatTiming]:
    return [
        BeatTiming(
            beat_id=1,
            start_word_id=1,
            end_word_id=3,
            start_seconds=0.0,
            end_seconds=1.5,
        ),
        BeatTiming(
            beat_id=2,
            start_word_id=4,
            end_word_id=5,
            start_seconds=1.5,
            end_seconds=3.0,
        ),
        BeatTiming(
            beat_id=3,
            start_word_id=6,
            end_word_id=7,
            start_seconds=3.0,
            end_seconds=4.0,
        ),
        BeatTiming(
            beat_id=4,
            start_word_id=8,
            end_word_id=9,
            start_seconds=4.0,
            end_seconds=5.5,
        ),
        BeatTiming(
            beat_id=5,
            start_word_id=10,
            end_word_id=11,
            start_seconds=5.5,
            end_seconds=6.0,
        ),
    ]


def _shots() -> list[Shot]:
    return [
        Shot(
            id=1,
            scene_id=1,
            beat_ids=[1, 2],
            entity_ids=[],
            action="Primera unidad visual.",
        ),
        Shot(
            id=2,
            scene_id=1,
            beat_ids=[3],
            entity_ids=[],
            action="Segunda unidad visual.",
        ),
        Shot(
            id=3,
            scene_id=2,
            beat_ids=[4, 5],
            entity_ids=[],
            action="Tercera unidad visual.",
        ),
    ]


def test_shot_timing_derives_contiguous_intervals_from_beat_ranges() -> None:
    timings = build_shot_timings(_shots(), _beat_timings())

    assert [timing.model_dump() for timing in timings] == [
        {"shot_id": 1, "start_seconds": 0.0, "end_seconds": 3.0},
        {"shot_id": 2, "start_seconds": 3.0, "end_seconds": 4.0},
        {"shot_id": 3, "start_seconds": 4.0, "end_seconds": 6.0},
    ]


def test_shot_timing_rejects_missing_or_duplicated_beat_coverage() -> None:
    shots = [
        Shot(
            id=1,
            scene_id=1,
            beat_ids=[1, 2],
            entity_ids=[],
            action="Primera unidad visual.",
        ),
        Shot(
            id=2,
            scene_id=1,
            beat_ids=[2, 3, 4, 5],
            entity_ids=[],
            action="Cobertura inválida.",
        ),
    ]

    with pytest.raises(ValueError, match="cover every timed beat exactly once"):
        build_shot_timings(shots, _beat_timings())


def test_shot_timing_rejects_nonconsecutive_beats_inside_shot() -> None:
    shots = [
        Shot(
            id=1,
            scene_id=1,
            beat_ids=[1, 3],
            entity_ids=[],
            action="Salta un beat.",
        ),
        Shot(
            id=2,
            scene_id=1,
            beat_ids=[2, 4, 5],
            entity_ids=[],
            action="Cobertura inválida.",
        ),
    ]

    with pytest.raises(ValueError, match="must contain consecutive beat IDs"):
        build_shot_timings(shots, _beat_timings())


def test_shot_timing_rejects_gap_in_beat_timeline() -> None:
    beat_timings = _beat_timings()
    beat_timings[2] = beat_timings[2].model_copy(update={"start_seconds": 3.2})

    with pytest.raises(ValueError, match="Beat timings must be contiguous"):
        build_shot_timings(_shots(), beat_timings)
