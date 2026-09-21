import re
from pathlib import Path

from ai_video_factory.domain import NarrationAudio, NarrationWord, SourceScript
from ai_video_factory.providers.transcription import TranscriptionProvider

_TIMESTAMP_TOLERANCE_SECONDS = 0.5
_ZERO_DURATION_TOLERANCE_SECONDS = 1e-6
_MAX_TRAILING_ZERO_DURATION_WORDS = 4
_MIN_WORD_COUNT_RATIO = 0.55
_MAX_WORD_COUNT_RATIO = 1.75
_MIN_SOURCE_WORDS_FOR_RATIO_CHECK = 20


async def align_narration_words(
    source: SourceScript,
    narration: NarrationAudio,
    audio: bytes,
    *,
    transcription_provider: TranscriptionProvider,
    model: str,
    language: str | None,
) -> list[NarrationWord]:
    """Extract validated word timestamps while preserving the source script as narrative truth."""

    if not audio:
        raise ValueError("Narration audio must not be empty")
    if not model.strip():
        raise ValueError("Transcription model must be non-empty")

    words = await transcription_provider.transcribe_words(
        audio,
        filename=Path(narration.uri).name,
        model=model,
        prompt="",
        language=language,
    )
    if not words:
        raise ValueError("Transcription provider returned no narration words")

    aligned: list[NarrationWord] = []
    previous_start = -1.0
    previous_end = -1.0

    for word_id, word in enumerate(words, start=1):
        text = " ".join(word.text.split())
        if not text:
            raise ValueError("Transcription provider returned an empty narration word")
        if word.start_seconds < 0 or word.end_seconds < word.start_seconds:
            raise ValueError(
                "Transcription provider returned invalid narration word timestamps: "
                f"word_id={word_id}, text={text!r}, "
                f"start={word.start_seconds}, end={word.end_seconds}"
            )
        if word.start_seconds < previous_start or word.end_seconds < previous_end:
            raise ValueError(
                "Narration word timestamps must be ordered: "
                f"word_id={word_id}, text={text!r}, "
                f"start={word.start_seconds}, end={word.end_seconds}, "
                f"previous_start={previous_start}, previous_end={previous_end}"
            )

        aligned.append(
            NarrationWord(
                id=word_id,
                text=text,
                start_seconds=word.start_seconds,
                end_seconds=word.end_seconds,
            )
        )
        previous_start = word.start_seconds
        previous_end = word.end_seconds

    if aligned[-1].end_seconds > narration.duration_seconds + _TIMESTAMP_TOLERANCE_SECONDS:
        raise ValueError(
            "Narration word timestamps exceed measured narration duration: "
            f"last_end={aligned[-1].end_seconds}, "
            f"duration={narration.duration_seconds}"
        )

    _validate_alignment_quality(source, aligned)
    return aligned


def _validate_alignment_quality(
    source: SourceScript,
    words: list[NarrationWord],
) -> None:
    source_word_count = len(_tokenize_source_words(source.text))
    aligned_word_count = len(words)

    if source_word_count >= _MIN_SOURCE_WORDS_FOR_RATIO_CHECK:
        ratio = aligned_word_count / source_word_count
        if ratio < _MIN_WORD_COUNT_RATIO or ratio > _MAX_WORD_COUNT_RATIO:
            raise ValueError(
                "Narration alignment word count is implausible relative to the source script: "
                f"source_words={source_word_count}, aligned_words={aligned_word_count}, "
                f"ratio={ratio:.3f}"
            )

    trailing_zero_duration = 0
    for word in reversed(words):
        if abs(word.end_seconds - word.start_seconds) > _ZERO_DURATION_TOLERANCE_SECONDS:
            break
        trailing_zero_duration += 1

    if trailing_zero_duration > _MAX_TRAILING_ZERO_DURATION_WORDS:
        first_bad = words[len(words) - trailing_zero_duration]
        raise ValueError(
            "Narration alignment has an implausible trailing zero-duration word run: "
            f"count={trailing_zero_duration}, first_word_id={first_bad.id}, "
            f"timestamp={first_bad.start_seconds:.3f}"
        )


def _tokenize_source_words(text: str) -> list[str]:
    return re.findall(r"\w+(?:['’]\w+)?", text.casefold(), flags=re.UNICODE)
