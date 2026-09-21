from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from ai_video_factory.workers.flux2_klein import FLUX2_KLEIN_MODEL_ID

from .ideogram_rejections import is_terminal_safety_rejection_detail
from .images import GeneratedImage, ImageFormat, ImageProvider, ImageQuality
from .inference_jobs import InferenceJobTimeoutError, RemoteInferenceRejectedError

logger = logging.getLogger(__name__)


class SafetyFallbackImageProvider:
    """Use FLUX for terminal safety rejects or a bounded primary timeout."""

    def __init__(
        self,
        *,
        primary: ImageProvider,
        fallback: ImageProvider,
        fallback_model: str = FLUX2_KLEIN_MODEL_ID,
        before_fallback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_model = fallback_model
        self._before_fallback = before_fallback
        self._fallback_prepare_lock = asyncio.Lock()
        self._fallback_prepared = before_fallback is None
        self._primary_state_lock = asyncio.Lock()
        self._primary_timed_out = False

    async def _ensure_fallback_ready(self) -> None:
        if self._fallback_prepared:
            return
        async with self._fallback_prepare_lock:
            if self._fallback_prepared:
                return
            if self._before_fallback is None:
                self._fallback_prepared = True
                return
            await self._before_fallback()
            self._fallback_prepared = True

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        size: str,
        quality: ImageQuality,
        output_format: ImageFormat,
    ) -> GeneratedImage:
        fallback_reason: str | None = None
        async with self._primary_state_lock:
            if self._primary_timed_out:
                fallback_reason = "primary_timeout"
            else:
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
                    fallback_reason = "safety_rejection"
                except InferenceJobTimeoutError as exc:
                    if not exc.job_id.startswith("ideogram-"):
                        raise
                    self._primary_timed_out = True
                    fallback_reason = "primary_timeout"
                    logger.warning(
                        "Primary Ideogram transport timed out for application_job_id=%s; "
                        "routing the remainder of this batch to FLUX.",
                        exc.job_id,
                    )

        await self._ensure_fallback_ready()
        image = await self._fallback.generate_image(
            prompt=prompt,
            model=self._fallback_model,
            size=size,
            quality=quality,
            output_format=output_format,
        )
        metadata = dict(image.metadata)
        metadata.setdefault("fallback_from", "ideogram4")
        metadata.setdefault("fallback_reason", fallback_reason)
        return replace(image, metadata=metadata)
