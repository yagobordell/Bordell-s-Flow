from __future__ import annotations

from typing import Protocol


class ImageUpscaler(Protocol):
    """Upscale an image to exact target dimensions."""

    def upscale(
        self,
        image_bytes: bytes,
        *,
        target_width: int,
        target_height: int,
    ) -> bytes: ...
