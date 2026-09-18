"""External model and API providers."""

from .base import StructuredTextProvider
from .images import (
    GeneratedImage,
    ImageProvider,
    ImageReferenceInput,
    ReferenceAwareImageProvider,
)
from .openai import OpenAIProvider
from .openai_images import OpenAIImageProvider
from .openai_speech import OpenAISpeechProvider
from .openai_transcription import OpenAITranscriptionProvider
from .safety_fallback import SafetyFallbackImageProvider
from .salad_breeze import SaladBreezeSpeechProvider
from .salad_whisper import SaladWhisperTranscriptionProvider
from .speech import GeneratedSpeech, SpeechProvider
from .transcription import TranscribedWord, TranscriptionProvider

__all__ = [
    "GeneratedImage",
    "GeneratedSpeech",
    "ImageProvider",
    "ImageReferenceInput",
    "OpenAIImageProvider",
    "OpenAIProvider",
    "OpenAISpeechProvider",
    "OpenAITranscriptionProvider",
    "ReferenceAwareImageProvider",
    "SafetyFallbackImageProvider",
    "SaladBreezeSpeechProvider",
    "SaladFlux2KleinImageProvider",
    "SaladIdeogramImageProvider",
    "SaladWhisperTranscriptionProvider",
    "SpeechProvider",
    "StructuredTextProvider",
    "TranscribedWord",
    "TranscriptionProvider",
]


def __getattr__(name: str):
    if name == "SaladIdeogramImageProvider":
        from .salad_ideogram import SaladIdeogramImageProvider

        return SaladIdeogramImageProvider
    if name == "SaladFlux2KleinImageProvider":
        from .salad_flux2 import SaladFlux2KleinImageProvider

        return SaladFlux2KleinImageProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
