from pathlib import Path

from ai_video_factory.domain import NarrationAudio, NarrationWord, SourceScript
from ai_video_factory.providers.transcription import TranscriptionProvider

_TIMESTAMP_TOLERANCE_SECONDS = 0.5


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
        prompt=source.text,
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

    return aligned
