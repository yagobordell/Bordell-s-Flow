from __future__ import annotations

from dataclasses import replace

from .salad_breeze import BreezeFallbackEligibleError
from .speech import GeneratedSpeech, SpeechFormat, SpeechProvider


class SpeechFallbackFailedError(RuntimeError):
    """Both providers failed after an explicitly eligible primary failure."""

    def __init__(
        self,
        *,
        primary_error: BreezeFallbackEligibleError,
        fallback_error: Exception,
    ) -> None:
        self.primary_error = primary_error
        self.fallback_error = fallback_error
        super().__init__(
            "Primary Breeze and fallback Fish both failed; "
            f"primary_reason={primary_error.reason}; fallback={fallback_error}"
        )


class BreezeThenFishSpeechProvider:
    """Provider-neutral primary/fallback policy; it never manipulates Salad control plane."""

    def __init__(
        self,
        *,
        primary: SpeechProvider,
        fallback: SpeechProvider,
        fallback_model: str,
        fallback_voice: str,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_model = fallback_model
        self._fallback_voice = fallback_voice

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
        try:
            return await self._primary.generate_speech(
                text=text,
                model=model,
                voice=voice,
                instructions=instructions,
                speed=speed,
                output_format=output_format,
            )
        except BreezeFallbackEligibleError as primary_error:
            try:
                generated = await self._fallback.generate_speech(
                    text=text,
                    model=self._fallback_model,
                    voice=self._fallback_voice,
                    instructions=instructions,
                    speed=speed,
                    output_format=output_format,
                )
            except Exception as fallback_error:
                raise SpeechFallbackFailedError(
                    primary_error=primary_error,
                    fallback_error=fallback_error,
                ) from fallback_error

            metadata = {
                **dict(generated.metadata),
                "fallback_from": "breeze_tts2",
                "fallback_reason": primary_error.reason,
            }
            return replace(generated, metadata=metadata)
