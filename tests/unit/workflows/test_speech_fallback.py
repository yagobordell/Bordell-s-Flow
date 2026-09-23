import asyncio

import pytest

from ai_video_factory.providers.fallback_speech import (
    BreezeThenFishSpeechProvider,
    SpeechFallbackFailedError,
)
from ai_video_factory.providers.salad_breeze import BreezeFallbackEligibleError
from ai_video_factory.providers.speech import GeneratedSpeech


class FakeProvider:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    async def generate_speech(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def _speech(provider: str) -> GeneratedSpeech:
    return GeneratedSpeech(
        content=b"wav",
        media_type="audio/wav",
        extension="wav",
        metadata={"provider": provider},
    )


def _run(provider):
    return asyncio.run(
        provider.generate_speech(
            text="Narration.",
            model="BreezeBlue/Breeze-TTS-2",
            voice="A warm narrator.",
            instructions="Measured.",
            speed=1.0,
            output_format="wav",
        )
    )


def test_breeze_success_never_calls_fish() -> None:
    primary = FakeProvider(result=_speech("breeze_tts2"))
    fallback = FakeProvider(result=_speech("fish_speech"))
    provider = BreezeThenFishSpeechProvider(
        primary=primary,
        fallback=fallback,
        fallback_model="fishaudio/s2-pro",
        fallback_voice="project-narrator-v1",
    )

    result = _run(provider)

    assert result.metadata["provider"] == "breeze_tts2"
    assert len(primary.calls) == 1
    assert fallback.calls == []


def test_eligible_breeze_failure_calls_fish_once_and_records_reason() -> None:
    primary = FakeProvider(
        error=BreezeFallbackEligibleError("breeze_pending_timeout", "timed out")
    )
    fallback = FakeProvider(result=_speech("fish_speech"))
    provider = BreezeThenFishSpeechProvider(
        primary=primary,
        fallback=fallback,
        fallback_model="fishaudio/s2-pro",
        fallback_voice="project-narrator-v1",
    )

    result = _run(provider)

    assert len(fallback.calls) == 1
    assert fallback.calls[0]["model"] == "fishaudio/s2-pro"
    assert fallback.calls[0]["voice"] == "project-narrator-v1"
    assert result.metadata["fallback_from"] == "breeze_tts2"
    assert result.metadata["fallback_reason"] == "breeze_pending_timeout"


def test_noneligible_breeze_failure_never_calls_fish() -> None:
    primary = FakeProvider(error=ValueError("invalid source contract"))
    fallback = FakeProvider(result=_speech("fish_speech"))
    provider = BreezeThenFishSpeechProvider(
        primary=primary,
        fallback=fallback,
        fallback_model="fishaudio/s2-pro",
        fallback_voice="project-narrator-v1",
    )

    with pytest.raises(ValueError, match="invalid source"):
        _run(provider)
    assert fallback.calls == []


def test_fish_failure_preserves_primary_and_fallback_context() -> None:
    primary_error = BreezeFallbackEligibleError(
        "breeze_worker_inference_rejection",
        "primary failed",
    )
    primary = FakeProvider(error=primary_error)
    fallback_error = RuntimeError("Fish failed")
    fallback = FakeProvider(error=fallback_error)
    provider = BreezeThenFishSpeechProvider(
        primary=primary,
        fallback=fallback,
        fallback_model="fishaudio/s2-pro",
        fallback_voice="project-narrator-v1",
    )

    with pytest.raises(SpeechFallbackFailedError) as captured:
        _run(provider)

    assert captured.value.primary_error is primary_error
    assert captured.value.fallback_error is fallback_error
    assert len(fallback.calls) == 1
