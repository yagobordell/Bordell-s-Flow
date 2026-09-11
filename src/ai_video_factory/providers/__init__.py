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
from .salad_breeze import SaladBreezeSpeechProvider
from .salad_ideogram import SaladIdeogramImageProvider
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
    "SaladBreezeSpeechProvider",
    "SaladIdeogramImageProvider",
    "SaladWhisperTranscriptionProvider",
    "SpeechProvider",
    "StructuredTextProvider",
    "TranscribedWord",
    "TranscriptionProvider",
]
