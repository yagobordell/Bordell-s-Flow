from __future__ import annotations

from ai_video_factory.domain import NarrationWord

from .models import CaptionCue, CaptionWord
from .timeline import seconds_to_frame

_TIME_TOLERANCE_SECONDS = 1e-6
_TERMINAL_PUNCTUATION = (".", "?", "!", "…")
_CLOSING_PUNCTUATION = "\"'”’)]}"


def build_caption_cues(
    words: list[NarrationWord],
    *,
    fps: int,
    total_frames: int,
    max_words: int = 5,
    max_chars: int = 36,
    max_duration_seconds: float = 2.5,
    pause_threshold_seconds: float = 0.35,
) -> list[CaptionCue]:
    """Group canonical narration words into deterministic frame-exact caption cues."""

    _validate_settings(
        fps=fps,
        total_frames=total_frames,
        max_words=max_words,
        max_chars=max_chars,
        max_duration_seconds=max_duration_seconds,
        pause_threshold_seconds=pause_threshold_seconds,
    )
    _validate_words(words, fps=fps, total_frames=total_frames)

    caption_words = [
        _quantize_word(word, fps=fps, total_frames=total_frames) for word in words
    ]
    groups: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []

    for source_word, caption_word in zip(words, caption_words, strict=True):
        if current and _should_start_new_cue(
            current,
            source_word,
            words=words,
            max_words=max_words,
            max_chars=max_chars,
            max_duration_seconds=max_duration_seconds,
            pause_threshold_seconds=pause_threshold_seconds,
        ):
            groups.append(current)
            current = []
        current.append(caption_word)

    if current:
        groups.append(current)

    return [
        CaptionCue(
            id=cue_id,
            text=" ".join(word.text for word in group),
            start_frame=group[0].start_frame,
            end_frame=group[-1].end_frame,
            word_ids=[word.word_id for word in group],
            words=group,
        )
        for cue_id, group in enumerate(groups, start=1)
    ]


def _should_start_new_cue(
    current: list[CaptionWord],
    next_source_word: NarrationWord,
    *,
    words: list[NarrationWord],
    max_words: int,
    max_chars: int,
    max_duration_seconds: float,
    pause_threshold_seconds: float,
) -> bool:
    previous_source = words[current[-1].word_id - 1]
    first_source = words[current[0].word_id - 1]

    if _ends_sentence(previous_source.text):
        return True
    if next_source_word.start_seconds - previous_source.end_seconds > pause_threshold_seconds:
        return True
    if len(current) >= max_words:
        return True

    candidate_text = " ".join([*(word.text for word in current), next_source_word.text])
    if len(candidate_text) > max_chars:
        return True
    return next_source_word.end_seconds - first_source.start_seconds > max_duration_seconds


def _quantize_word(
    word: NarrationWord,
    *,
    fps: int,
    total_frames: int,
) -> CaptionWord:
    start_frame = seconds_to_frame(word.start_seconds, fps)
    if start_frame >= total_frames:
        raise ValueError(
            f"Narration word {word.id} starts outside the composition timeline: "
            f"start_frame={start_frame}, total_frames={total_frames}"
        )

    end_frame = max(seconds_to_frame(word.end_seconds, fps), start_frame + 1)
    end_frame = min(end_frame, total_frames)
    if end_frame <= start_frame:  # pragma: no cover - guarded by start_frame check
        raise ValueError(f"Narration word {word.id} has no visible frame interval")

    return CaptionWord(
        word_id=word.id,
        text=word.text,
        start_frame=start_frame,
        end_frame=end_frame,
    )


def _validate_settings(
    *,
    fps: int,
    total_frames: int,
    max_words: int,
    max_chars: int,
    max_duration_seconds: float,
    pause_threshold_seconds: float,
) -> None:
    if fps <= 0 or total_frames <= 0:
        raise ValueError("Caption fps and total_frames must be positive")
    if max_words <= 0 or max_chars <= 0:
        raise ValueError("Caption max_words and max_chars must be positive")
    if max_duration_seconds <= 0:
        raise ValueError("Caption max_duration_seconds must be positive")
    if pause_threshold_seconds < 0:
        raise ValueError("Caption pause_threshold_seconds must be >= 0")


def _validate_words(
    words: list[NarrationWord],
    *,
    fps: int,
    total_frames: int,
) -> None:
    if not words:
        raise ValueError("Caption planning requires at least one narration word")

    word_ids = [word.id for word in words]
    if word_ids != list(range(1, len(words) + 1)):
        raise ValueError("Narration words must have consecutive IDs starting at 1")

    previous_start = -1.0
    previous_end = -1.0
    for word in words:
        if word.end_seconds < word.start_seconds:
            raise ValueError(f"Narration word {word.id} end must not precede its start")
        if (
            word.start_seconds + _TIME_TOLERANCE_SECONDS < previous_start
            or word.end_seconds + _TIME_TOLERANCE_SECONDS < previous_end
        ):
            raise ValueError("Narration word timestamps must be ordered")
        previous_start = word.start_seconds
        previous_end = word.end_seconds

    timeline_seconds = total_frames / fps
    if words[0].start_seconds > timeline_seconds + _TIME_TOLERANCE_SECONDS:
        raise ValueError("Narration words start after the composition timeline")


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip(_CLOSING_PUNCTUATION)
    return stripped.endswith(_TERMINAL_PUNCTUATION)
