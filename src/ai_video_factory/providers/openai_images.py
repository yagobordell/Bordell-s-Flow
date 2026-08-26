import base64
import binascii
from typing import TYPE_CHECKING, Any

from ai_video_factory.providers.images import (
    GeneratedImage,
    ImageFormat,
    ImageInputFidelity,
    ImageQuality,
    ImageReferenceInput,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI


_MEDIA_TYPES: dict[ImageFormat, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

_EXTENSION_BY_MEDIA_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
}


class OpenAIImageProvider:
    """OpenAI implementation of provider-neutral image generation contracts."""

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
        return _decode_response_image(response, output_format, operation="generation")

    async def generate_image_with_references(
        self,
        *,
        prompt: str,
        references: list[ImageReferenceInput],
        model: str,
        size: str,
        quality: ImageQuality,
        output_format: ImageFormat,
        input_fidelity: ImageInputFidelity | None = None,
    ) -> GeneratedImage:
        if not references:
            return await self.generate_image(
                prompt=prompt,
                model=model,
                size=size,
                quality=quality,
                output_format=output_format,
            )

        image_files = []
        for index, reference in enumerate(references, start=1):
            extension = _EXTENSION_BY_MEDIA_TYPE.get(reference.media_type)
            if extension is None:
                raise ValueError(
                    f"Unsupported image reference media type: {reference.media_type}"
                )
            image_files.append(
                (
                    f"reference_{index:03d}.{extension}",
                    reference.content,
                    reference.media_type,
                )
            )

        edit_kwargs: dict[str, Any] = {
            "image": image_files,
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
            "output_format": output_format,
        }
        if input_fidelity is not None:
            edit_kwargs["input_fidelity"] = input_fidelity

        response = await self._client.images.edit(**edit_kwargs)
        return _decode_response_image(response, output_format, operation="edit")


def _decode_response_image(
    response: Any,
    output_format: ImageFormat,
    *,
    operation: str,
) -> GeneratedImage:
    data = response.data
    if not data or len(data) != 1:
        raise RuntimeError(f"OpenAI image {operation} did not return exactly one image")

    encoded = getattr(data[0], "b64_json", None)
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError(f"OpenAI image {operation} returned no base64 image payload")

    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"OpenAI image {operation} returned invalid base64 data") from exc

    if not content:
        raise RuntimeError(f"OpenAI image {operation} returned an empty image payload")

    return GeneratedImage(
        content=content,
        media_type=_MEDIA_TYPES[output_format],
        extension=output_format,
    )
