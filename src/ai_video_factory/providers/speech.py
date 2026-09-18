from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

type SpeechFormat = Literal["wav"]


@dataclass(frozen=True)
class GeneratedSpeech:
    """Provider speech result kept in memory until the application validates and persists it."""

    content: bytes
    media_type: str
    extension: SpeechFormat
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SpeechProvider(Protocol):
    """Provider-neutral contract for generating narration speech from immutable script text."""

    async def generate_speech(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        instructions: str,
        speed: float,
        output_format: SpeechFormat,
    ) -> GeneratedSpeech:
        """Generate one speech asset from the provided text."""
        ...
