import pytest

from ai_video_factory.compositor import build_caption_cues
from ai_video_factory.domain import NarrationWord


def _word(word_id: int, text: str, start: float, end: float) -> NarrationWord:
    return NarrationWord(
        id=word_id,
        text=text,
        start_seconds=start,
        end_seconds=end,
    )


def test_balances_soft_splits_instead_of_creating_orphan_words() -> None:
    words = [
        _word(1, "Japón", 0.00, 0.20),
        _word(2, "fue", 0.22, 0.40),
        _word(3, "gobernado", 0.42, 0.80),
        _word(4, "por", 0.82, 0.95),
        _word(5, "guerreros", 0.97, 1.25),
        _word(6, "durante", 1.27, 1.55),
    ]

    cues = build_caption_cues(words, fps=24, total_frames=48)

    assert [cue.text for cue in cues] == ["Japón fue gobernado", "por guerreros durante"]
    assert [cue.word_ids for cue in cues] == [[1, 2, 3], [4, 5, 6]]
    assert [(word.start_frame, word.end_frame) for word in cues[0].words] == [
        (0, 5),
        (5, 10),
        (10, 19),
    ]
    assert cues[1].start_frame == 20
    assert cues[1].end_frame == 37


def test_starts_new_cue_after_long_pause() -> None:
    words = [
        _word(1, "una", 0.0, 0.2),
        _word(2, "idea", 0.2, 0.4),
        _word(3, "otra", 0.9, 1.1),
    ]

    cues = build_caption_cues(words, fps=24, total_frames=48)

    assert [cue.word_ids for cue in cues] == [[1, 2], [3]]


def test_starts_new_cue_after_terminal_punctuation() -> None:
    words = [
        _word(1, "Cambió", 0.0, 0.2),
        _word(2, "todo.", 0.2, 0.5),
        _word(3, "Después", 0.5, 0.8),
    ]

    cues = build_caption_cues(words, fps=24, total_frames=48)

    assert [cue.text for cue in cues] == ["Cambió todo.", "Después"]


def test_starts_new_cue_before_character_limit_is_exceeded() -> None:
    words = [
        _word(1, "1234567890", 0.0, 0.2),
        _word(2, "abcdefghij", 0.2, 0.4),
        _word(3, "ABCDEFGHIJ", 0.4, 0.6),
    ]

    cues = build_caption_cues(words, fps=24, total_frames=48, max_chars=25)

    assert [cue.word_ids for cue in cues] == [[1, 2], [3]]


def test_normalizes_frame_overlap_between_words_and_cues() -> None:
    words = [
        _word(1, "uno", 0.0, 0.5),
        _word(2, "dos", 0.45, 0.8),
    ]

    cues = build_caption_cues(
        words,
        fps=24,
        total_frames=24,
        max_words=1,
    )

    assert cues[0].words[0].start_frame == 0
    assert cues[0].words[0].end_frame == 12
    assert cues[1].words[0].start_frame == 12
    assert cues[1].words[0].end_frame == 19
    assert cues[0].end_frame == cues[1].start_frame


def test_zero_duration_word_receives_one_visible_frame() -> None:
    cues = build_caption_cues(
        [_word(1, "Japón", 0.0, 0.0), _word(2, "feudal", 0.2, 0.5)],
        fps=24,
        total_frames=24,
    )

    assert cues[0].words[0].start_frame == 0
    assert cues[0].words[0].end_frame == 1


def test_rejects_word_starting_outside_timeline() -> None:
    with pytest.raises(ValueError, match="starts outside"):
        build_caption_cues(
            [_word(1, "fin", 1.0, 1.0)],
            fps=24,
            total_frames=24,
        )


def test_rejects_non_consecutive_word_ids() -> None:
    with pytest.raises(ValueError, match="consecutive IDs"):
        build_caption_cues(
            [_word(2, "dos", 0.0, 0.2)],
            fps=24,
            total_frames=24,
        )
