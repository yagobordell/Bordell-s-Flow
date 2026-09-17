from __future__ import annotations

from dataclasses import replace

from .ideogram_rejections import is_terminal_ideogram_safety_rejection
from .images import GeneratedImage, ImageFormat, ImageProvider, ImageQuality
from .inference_jobs import RemoteInferenceRejectedError


class SafetyFallbackImageProvider:
    """Use a secondary provider only for confirmed terminal Ideogram safety rejections."""

    def __init__(
        self,
        *,
        primary: ImageProvider,
        fallback: ImageProvider,
        fallback_model: str,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_model = fallback_model

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        size: str,
        quality: ImageQuality,
        output_format: ImageFormat,
    ) -> GeneratedImage:
        try:
            return await self._primary.generate_image(
                prompt=prompt,
                model=model,
                size=size,
                quality=quality,
                output_format=output_format,
            )
        except RemoteInferenceRejectedError as exc:
            if not is_terminal_ideogram_safety_rejection(exc.detail):
                raise

        result = await self._fallback.generate_image(
            prompt=prompt,
            model=self._fallback_model,
            size=size,
            quality=quality,
            output_format=output_format,
        )
        metadata = dict(result.metadata)
        metadata.update(
            {
                "fallback_from": "ideogram4",
                "fallback_reason": "safety_rejection",
            }
        )
        return replace(result, metadata=metadata)
