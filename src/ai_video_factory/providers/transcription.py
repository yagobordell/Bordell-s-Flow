from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TranscribedWord:
    """Provider word timestamp kept in memory until application validation."""

    text: str
    start_seconds: float
    end_seconds: float


class TranscriptionProvider(Protocol):
    """Provider-neutral contract for extracting word timestamps from narration audio."""

    async def transcribe_words(
        self,
        audio: bytes,
        *,
        filename: str,
        model: str,
        prompt: str,
        language: str | None,
    ) -> list[TranscribedWord]:
        """Return recognized words with provider-measured timestamps."""
        ...
