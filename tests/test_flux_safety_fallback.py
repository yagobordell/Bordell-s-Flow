import asyncio
from typing import Any

import pytest

from ai_video_factory.providers.image_fallback import SafetyFallbackImageProvider
from ai_video_factory.providers.images import GeneratedImage
from ai_video_factory.providers.inference_jobs import RemoteInferenceRejectedError
from ai_video_factory.providers.salad_flux import render_flux_prompt


class FakeProvider:
    def __init__(self, result: GeneratedImage | BaseException) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _image(provider: str) -> GeneratedImage:
    return GeneratedImage(
        content=b"png",
        media_type="image/png",
        extension="png",
        metadata={"provider": provider},
    )


def _caption() -> str:
    return (
        '{"high_level_description":"Canonical location reference. Distant rocky hills.",'
        '"style_description":{"aesthetics":"cinematic documentary",'
        '"lighting":"neutral daylight","photo":"realistic photography",'
        '"medium":"documentary photograph"},'
        '"compositional_deconstruction":{"background":"Stable desert horizon.",'
        '"elements":[]}}'
    )


def test_safety_fallback_uses_secondary_only_after_terminal_ideogram_rejection() -> None:
    primary = FakeProvider(
        RemoteInferenceRejectedError(
            "ideogram-reference-deadbeef",
            "Ideogram 4 safety filter blocked all provider caption variants",
        )
    )
    fallback = FakeProvider(_image("flux_schnell"))
    provider = SafetyFallbackImageProvider(
        primary=primary,  # type: ignore[arg-type]
        fallback=fallback,  # type: ignore[arg-type]
        fallback_model="black-forest-labs/FLUX.1-schnell",
    )

    result = asyncio.run(
        provider.generate_image(
            prompt=_caption(),
            model="ideogram-ai/ideogram-4-nf4",
            size="1024x1024",
            quality="high",
            output_format="png",
        )
    )

    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1
    assert fallback.calls[0]["model"] == "black-forest-labs/FLUX.1-schnell"
    assert result.metadata == {
        "provider": "flux_schnell",
        "fallback_from": "ideogram4",
        "fallback_reason": "safety_rejection",
    }


def test_safety_fallback_does_not_hide_non_safety_rejections() -> None:
    primary = FakeProvider(
        RemoteInferenceRejectedError(
            "ideogram-reference-deadbeef",
            "worker rejected malformed request",
        )
    )
    fallback = FakeProvider(_image("flux_schnell"))
    provider = SafetyFallbackImageProvider(
        primary=primary,  # type: ignore[arg-type]
        fallback=fallback,  # type: ignore[arg-type]
        fallback_model="black-forest-labs/FLUX.1-schnell",
    )

    with pytest.raises(RemoteInferenceRejectedError, match="malformed request"):
        asyncio.run(
            provider.generate_image(
                prompt=_caption(),
                model="ideogram-ai/ideogram-4-nf4",
                size="1024x1024",
                quality="high",
                output_format="png",
            )
        )

    assert fallback.calls == []


def test_safety_fallback_does_not_hide_infrastructure_errors() -> None:
    primary = FakeProvider(TimeoutError("queue timed out"))
    fallback = FakeProvider(_image("flux_schnell"))
    provider = SafetyFallbackImageProvider(
        primary=primary,  # type: ignore[arg-type]
        fallback=fallback,  # type: ignore[arg-type]
        fallback_model="black-forest-labs/FLUX.1-schnell",
    )

    with pytest.raises(TimeoutError, match="queue timed out"):
        asyncio.run(
            provider.generate_image(
                prompt=_caption(),
                model="ideogram-ai/ideogram-4-nf4",
                size="1024x1024",
                quality="high",
                output_format="png",
            )
        )

    assert fallback.calls == []


def test_flux_prompt_rendering_is_deterministic_and_removes_json_transport() -> None:
    first = render_flux_prompt(_caption())
    second = render_flux_prompt(_caption())

    assert first == second
    assert "Distant rocky hills" in first
    assert "Style:" in first
    assert "Stable desert horizon" in first
    assert "No text, captions, logos" in first
    assert "high_level_description" not in first
