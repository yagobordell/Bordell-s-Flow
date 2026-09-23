"""External model and API providers."""

from .base import StructuredTextProvider
from .fallback_speech import BreezeThenFishSpeechProvider, SpeechFallbackFailedError
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
from .salad_breeze import BreezeFallbackEligibleError, SaladBreezeSpeechProvider
from .salad_fish_speech import FishSpeechReference, SaladFishSpeechProvider
from .salad_whisper import SaladWhisperTranscriptionProvider
from .speech import GeneratedSpeech, SpeechProvider
from .transcription import TranscribedWord, TranscriptionProvider

__all__ = [
    "BreezeFallbackEligibleError",
    "BreezeThenFishSpeechProvider",
    "FishSpeechReference",
    "GeneratedImage",
    "GeneratedSpeech",
    "ImageProvider",
    "ImageReferenceInput",
    "OpenAIImageProvider",
    "OpenAIProvider",
    "OpenAISpeechProvider",
    "OpenAITranscriptionProvider",
    "ReferenceAwareImageProvider",
    "SaladBreezeSpeechProvider",
    "SaladFishSpeechProvider",
    "SaladIdeogramImageProvider",
    "SaladQwenImage21Provider",
    "SaladWhisperTranscriptionProvider",
    "SpeechFallbackFailedError",
    "SpeechProvider",
    "StructuredTextProvider",
    "TranscribedWord",
    "TranscriptionProvider",
]


def __getattr__(name: str):
    if name == "SaladIdeogramImageProvider":
        from .salad_ideogram import SaladIdeogramImageProvider

        return SaladIdeogramImageProvider
    if name == "SaladQwenImage21Provider":
        from .salad_qwen_image import SaladQwenImage21Provider

        return SaladQwenImage21Provider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
