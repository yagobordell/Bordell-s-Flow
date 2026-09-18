import asyncio

import pytest

from ai_video_factory.providers.images import GeneratedImage
from ai_video_factory.providers.inference_jobs import RemoteInferenceRejectedError
from ai_video_factory.providers.safety_fallback import SafetyFallbackImageProvider


class _Provider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _image() -> GeneratedImage:
    return GeneratedImage(
        content=b"png",
        media_type="image/png",
        extension="png",
        metadata={"provider": "flux2_klein"},
    )


def test_terminal_ideogram_safety_rejection_uses_flux() -> None:
    primary = _Provider(
        RemoteInferenceRejectedError(
            "ideogram-reference-test",
            "Ideogram 4 safety filter blocked all provider caption variants",
        )
    )
    fallback = _Provider(_image())
    provider = SafetyFallbackImageProvider(primary=primary, fallback=fallback)

    result = asyncio.run(
        provider.generate_image(
            prompt="safe landscape",
            model="ideogram-ai/ideogram-4-nf4",
            size="1024x1024",
            quality="high",
            output_format="png",
        )
    )

    assert result.metadata["provider"] == "flux2_klein"
    assert result.metadata["fallback_from"] == "ideogram4"
    assert result.metadata["fallback_reason"] == "safety_rejection"
    assert fallback.calls[0]["model"] == "black-forest-labs/FLUX.2-klein-4B"


def test_non_safety_rejection_does_not_use_flux() -> None:
    rejection = RemoteInferenceRejectedError("job", "unsupported request")
    primary = _Provider(rejection)
    fallback = _Provider(_image())
    provider = SafetyFallbackImageProvider(primary=primary, fallback=fallback)

    with pytest.raises(RemoteInferenceRejectedError, match="unsupported request"):
        asyncio.run(
            provider.generate_image(
                prompt="prompt",
                model="ideogram-ai/ideogram-4-nf4",
                size="1024x1024",
                quality="high",
                output_format="png",
            )
        )

    assert fallback.calls == []
