"""External model and API providers."""

from .base import StructuredTextProvider
from .fallback_speech import BreezeThenFishSpeechProvider, SpeechFallbackFailedError
from .gemini_tts import GeminiTtsSpeechProvider
from .images import (
    GeneratedImage,
    ImageProvider,
    ImageReferenceInput,
    ReferenceAwareImageProvider,
)
from .openai import OpenAIProvider
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
    "GeminiTtsSpeechProvider",
    "ImageProvider",
    "ImageReferenceInput",
    "ImageUpscalerError",
    "OpenAIProvider",
    "OptiumLabsImageUpscaler",
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
    "create_optium_image_upscaler",
]


def __getattr__(name: str):
    if name == "SaladIdeogramImageProvider":
        from .salad_ideogram import SaladIdeogramImageProvider

        return SaladIdeogramImageProvider
    if name == "SaladQwenImage21Provider":
        from .salad_qwen_image import SaladQwenImage21Provider

        return SaladQwenImage21Provider
    if name in {"ImageUpscalerError", "OptiumLabsImageUpscaler", "create_optium_image_upscaler"}:
        from .optium_image_upscaler import (
            ImageUpscalerError,
            OptiumLabsImageUpscaler,
            create_optium_image_upscaler,
        )

        return {
            "ImageUpscalerError": ImageUpscalerError,
            "OptiumLabsImageUpscaler": OptiumLabsImageUpscaler,
            "create_optium_image_upscaler": create_optium_image_upscaler,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
