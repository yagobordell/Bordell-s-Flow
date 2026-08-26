from dataclasses import dataclass
from typing import Literal, Protocol

type ImageQuality = Literal["low", "medium", "high", "auto"]
type ImageFormat = Literal["png", "jpeg", "webp"]


@dataclass(frozen=True)
class GeneratedImage:
    """Provider result kept in memory until the application persists the image."""

    content: bytes
    media_type: str
    extension: ImageFormat


class ImageProvider(Protocol):
    """Provider-neutral contract for generating one image from one prompt."""

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        size: str,
        quality: ImageQuality,
        output_format: ImageFormat,
    ) -> GeneratedImage:
        """Generate one image and return its decoded binary payload."""
        ...
