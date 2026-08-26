from dataclasses import dataclass
from typing import Literal, Protocol

type ImageQuality = Literal["low", "medium", "high", "auto"]
type ImageFormat = Literal["png", "jpeg", "webp"]
type ImageInputFidelity = Literal["low", "high"]


@dataclass(frozen=True)
class GeneratedImage:
    """Provider result kept in memory until the application persists the image."""

    content: bytes
    media_type: str
    extension: ImageFormat


@dataclass(frozen=True)
class ImageReferenceInput:
    """Provider-neutral binary image used only as generation-time visual evidence."""

    content: bytes
    media_type: str


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


class ReferenceAwareImageProvider(ImageProvider, Protocol):
    """Image provider that can condition a new image on one or more visual references."""

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
        """Generate one image while preserving relevant identity from reference inputs."""
        ...
