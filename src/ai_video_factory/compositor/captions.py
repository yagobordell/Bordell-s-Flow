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

    caption_words = _quantize_words(words, fps=fps, total_frames=total_frames)
    groups: list[list[CaptionWord]] = []

    for source_segment, caption_segment in _split_segments(
        words,
        caption_words,
        pause_threshold_seconds=pause_threshold_seconds,
    ):
        groups.extend(
            _partition_segment(
                source_segment,
                caption_segment,
                max_words=max_words,
                max_chars=max_chars,
                max_duration_seconds=max_duration_seconds,
            )
        )

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


def _split_segments(
    source_words: list[NarrationWord],
    caption_words: list[CaptionWord],
    *,
    pause_threshold_seconds: float,
) -> list[tuple[list[NarrationWord], list[CaptionWord]]]:
    segments: list[tuple[list[NarrationWord], list[CaptionWord]]] = []
    start = 0

    for index, (current, following) in enumerate(
        zip(source_words, source_words[1:], strict=False)
    ):
        pause_seconds = following.start_seconds - current.end_seconds
        if _ends_sentence(current.text) or pause_seconds > pause_threshold_seconds:
            end = index + 1
            segments.append((source_words[start:end], caption_words[start:end]))
            start = end

    segments.append((source_words[start:], caption_words[start:]))
    return segments


def _partition_segment(
    source_words: list[NarrationWord],
    caption_words: list[CaptionWord],
    *,
    max_words: int,
    max_chars: int,
    max_duration_seconds: float,
) -> list[list[CaptionWord]]:
    count = len(source_words)
    target_words = min(4, max_words)
    best: list[list[tuple[int, int]] | None] = [None] * (count + 1)
    best[count] = []

    for start in range(count - 1, -1, -1):
        best_partition: list[tuple[int, int]] | None = None
        best_score: tuple[int, int, int, tuple[int, ...]] | None = None

        for end in range(start + 1, min(count, start + max_words) + 1):
            if not _cue_fits(
                source_words[start:end],
                max_chars=max_chars,
                max_duration_seconds=max_duration_seconds,
            ):
                continue
            remainder = best[end]
            if remainder is None:
                continue

            candidate = [(start, end), *remainder]
            score = _partition_score(candidate, target_words=target_words, segment_size=count)
            if best_score is None or score < best_score:
                best_partition = candidate
                best_score = score

        best[start] = best_partition

    partition = best[0]
    if partition is None:  # pragma: no cover - single words always remain feasible
        raise RuntimeError("Caption segment could not be partitioned")
    return [caption_words[start:end] for start, end in partition]


def _cue_fits(
    words: list[NarrationWord],
    *,
    max_chars: int,
    max_duration_seconds: float,
) -> bool:
    if len(words) == 1:
        return True

    text = " ".join(word.text for word in words)
    duration_seconds = words[-1].end_seconds - words[0].start_seconds
    return len(text) <= max_chars and duration_seconds <= max_duration_seconds


def _partition_score(
    groups: list[tuple[int, int]],
    *,
    target_words: int,
    segment_size: int,
) -> tuple[int, int, int, tuple[int, ...]]:
    lengths = [end - start for start, end in groups]
    singleton_count = sum(length == 1 for length in lengths) if segment_size > 1 else 0
    balance_penalty = sum((length - target_words) ** 2 for length in lengths)
    prefer_longer_early = tuple(-length for length in lengths)
    return len(groups), singleton_count, balance_penalty, prefer_longer_early


def _quantize_words(
    words: list[NarrationWord],
    *,
    fps: int,
    total_frames: int,
) -> list[CaptionWord]:
    raw_words = [_quantize_word(word, fps=fps, total_frames=total_frames) for word in words]
    normalized: list[CaptionWord] = []
    previous_end = 0

    for raw in raw_words:
        start_frame = max(raw.start_frame, previous_end)
        if start_frame >= total_frames:
            raise ValueError(
                f"Narration word {raw.word_id} cannot receive a non-overlapping visible frame"
            )
        end_frame = min(max(raw.end_frame, start_frame + 1), total_frames)
        normalized.append(
            CaptionWord(
                word_id=raw.word_id,
                text=raw.text,
                start_frame=start_frame,
                end_frame=end_frame,
            )
        )
        previous_end = end_frame

    return normalized


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
