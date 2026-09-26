from ai_video_factory.legacy_bots.beat_timing import BeatTimingBot
from ai_video_factory.domain import Beat, BeatTiming, NarrationAudio, NarrationWord, SourceScript


async def build_beat_timings(
    source: SourceScript,
    beats: list[Beat],
    words: list[NarrationWord],
    narration: NarrationAudio,
    *,
    timing_bot: BeatTimingBot,
) -> list[BeatTiming]:
    """Map ordered beats to contiguous narration intervals using semantic word boundaries."""

    _validate_inputs(beats, words, narration)
    beat_end_word_ids = await timing_bot.run(source, beats, words)

    words_by_id = {word.id: word for word in words}
    timings: list[BeatTiming] = []
    previous_end_word_id = 0
    previous_end_seconds = 0.0

    for index, (beat, end_word_id) in enumerate(
        zip(beats, beat_end_word_ids, strict=True),
        start=1,
    ):
        start_word_id = previous_end_word_id + 1
        if index < len(beats):
            next_word = words_by_id[end_word_id + 1]
            end_seconds = next_word.start_seconds
        else:
            end_seconds = narration.duration_seconds

        if end_word_id < start_word_id:
            raise ValueError("Beat timing boundary produced an empty narration word range")
        if end_seconds <= previous_end_seconds:
            raise ValueError(
                "Beat timing boundary produced a non-positive interval: "
                f"beat_id={beat.id}, start={previous_end_seconds}, end={end_seconds}"
            )

        timings.append(
            BeatTiming(
                beat_id=beat.id,
                start_word_id=start_word_id,
                end_word_id=end_word_id,
                start_seconds=previous_end_seconds,
                end_seconds=end_seconds,
            )
        )
        previous_end_word_id = end_word_id
        previous_end_seconds = end_seconds

    if timings[-1].end_word_id != words[-1].id:
        raise ValueError("Beat timings must consume every narration word exactly once")
    if timings[-1].end_seconds != narration.duration_seconds:
        raise ValueError("Beat timings must cover the full narration duration")

    return timings


def _validate_inputs(
    beats: list[Beat],
    words: list[NarrationWord],
    narration: NarrationAudio,
) -> None:
    if not beats:
        raise ValueError("Beat timing requires at least one beat")
    if not words:
        raise ValueError("Beat timing requires at least one narration word")

    beat_ids = [beat.id for beat in beats]
    if len(beat_ids) != len(set(beat_ids)) or beat_ids != sorted(beat_ids):
        raise ValueError("Beats must have unique ascending IDs")

    word_ids = [word.id for word in words]
    if word_ids != list(range(1, len(words) + 1)):
        raise ValueError("Narration words must have consecutive IDs starting at 1")

    previous_start = -1.0
    previous_end = -1.0
    for word in words:
        if word.end_seconds < word.start_seconds:
            raise ValueError("Narration word end timestamp cannot precede its start timestamp")
        if word.start_seconds < previous_start or word.end_seconds < previous_end:
            raise ValueError("Narration word timestamps must be ordered")
        previous_start = word.start_seconds
        previous_end = word.end_seconds

    if words[-1].end_seconds > narration.duration_seconds + 0.5:
        raise ValueError("Narration words exceed measured narration duration")
