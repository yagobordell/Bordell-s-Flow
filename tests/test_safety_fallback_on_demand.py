import asyncio

from ai_video_factory.providers.images import GeneratedImage
from ai_video_factory.providers.inference_jobs import RemoteInferenceRejectedError
from ai_video_factory.providers.safety_fallback import SafetyFallbackImageProvider


class RejectingProvider:
    async def generate_image(self, **_: object) -> GeneratedImage:
        raise RemoteInferenceRejectedError(
            "ideogram-test",
            "Ideogram 4 safety filter blocked all provider caption variants",
        )


class SuccessfulProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_image(self, **_: object) -> GeneratedImage:
        self.calls += 1
        return GeneratedImage(
            content=b"png",
            media_type="image/png",
            extension="png",
        )


def test_on_demand_fallback_prepares_once_for_concurrent_safety_rejections() -> None:
    async def run() -> None:
        fallback = SuccessfulProvider()
        prepare_calls = 0

        async def prepare() -> None:
            nonlocal prepare_calls
            prepare_calls += 1
            await asyncio.sleep(0)

        provider = SafetyFallbackImageProvider(
            primary=RejectingProvider(),
            fallback=fallback,
            before_fallback=prepare,
        )
        results = await asyncio.gather(
            provider.generate_image(
                prompt="a",
                model="ideogram",
                size="1024x1024",
                quality="high",
                output_format="png",
            ),
            provider.generate_image(
                prompt="b",
                model="ideogram",
                size="1024x1024",
                quality="high",
                output_format="png",
            ),
        )

        assert prepare_calls == 1
        assert fallback.calls == 2
        assert all(result.metadata["fallback_reason"] == "safety_rejection" for result in results)

    asyncio.run(run())
