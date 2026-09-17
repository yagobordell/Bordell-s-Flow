from __future__ import annotations

from dataclasses import replace

from ai_video_factory.workers.flux_schnell import FLUX_SCHNELL_MODEL_ID

from .ideogram_rejections import is_terminal_safety_rejection_detail
from .images import GeneratedImage, ImageFormat, ImageProvider, ImageQuality
from .inference_jobs import RemoteInferenceRejectedError


class SafetyFallbackImageProvider:
    """Use fallback only for confirmed terminal safety rejection by the primary provider."""

    def __init__(
        self,
        *,
        primary: ImageProvider,
        fallback: ImageProvider,
        fallback_model: str = FLUX_SCHNELL_MODEL_ID,
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
            if not is_terminal_safety_rejection_detail(exc.detail):
                raise
            image = await self._fallback.generate_image(
                prompt=prompt,
                model=self._fallback_model,
                size=size,
                quality=quality,
                output_format=output_format,
            )
            metadata = dict(image.metadata)
            metadata.setdefault("fallback_from", "ideogram4")
            metadata.setdefault("fallback_reason", "safety_rejection")
            return replace(image, metadata=metadata)
