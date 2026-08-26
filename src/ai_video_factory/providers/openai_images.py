import base64
import binascii
from typing import TYPE_CHECKING, Any

from ai_video_factory.providers.images import (
    GeneratedImage,
    ImageFormat,
    ImageQuality,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI


_MEDIA_TYPES: dict[ImageFormat, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


class OpenAIImageProvider:
    """OpenAI implementation of the provider-neutral image generation contract."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: "AsyncOpenAI | Any | None" = None,
    ) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)

        self._client = client

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        size: str,
        quality: ImageQuality,
        output_format: ImageFormat,
    ) -> GeneratedImage:
        response = await self._client.images.generate(
            model=model,
            prompt=prompt,
            n=1,
            size=size,
            quality=quality,
            output_format=output_format,
        )

        data = response.data
        if not data or len(data) != 1:
            raise RuntimeError("OpenAI image generation did not return exactly one image")

        encoded = getattr(data[0], "b64_json", None)
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("OpenAI image generation returned no base64 image payload")

        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("OpenAI image generation returned invalid base64 data") from exc

        if not content:
            raise RuntimeError("OpenAI image generation returned an empty image payload")

        return GeneratedImage(
            content=content,
            media_type=_MEDIA_TYPES[output_format],
            extension=output_format,
        )
