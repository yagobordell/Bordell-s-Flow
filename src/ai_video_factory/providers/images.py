from dataclasses import dataclass, field
from io import BytesIO
from typing import Literal, Protocol

from PIL import Image, UnidentifiedImageError

type ImageQuality = Literal["low", "medium", "high", "auto"]
type ImageFormat = Literal["png", "jpeg", "webp"]
type ImageInputFidelity = Literal["low", "high"]


@dataclass(frozen=True)
class GeneratedImage:
    """Provider result kept in memory until the application persists the image."""

    content: bytes
    media_type: str
    extension: ImageFormat
    metadata: dict[str, str] = field(default_factory=dict)


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


def parse_image_size(size: str) -> tuple[int, int]:
    """Parse the shared WIDTHxHEIGHT image-size notation."""

    parts = size.lower().split("x", maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Image size must use WIDTHxHEIGHT notation")
    try:
        width, height = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("Image size must contain integer dimensions") from exc
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")
    return width, height


def inspect_image_payload(content: bytes) -> tuple[str, int, int]:
    """Decode an image payload enough to prove its format and pixel dimensions."""

    if not content:
        raise ValueError("Image payload must not be empty")
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            image_format = (image.format or "").lower()
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Image payload could not be decoded") from exc
    if not image_format:
        raise ValueError("Image payload has no detectable format")
    return image_format, width, height


def require_generated_image_geometry(
    image: GeneratedImage,
    *,
    size: str,
    label: str,
) -> tuple[int, int]:
    """Reject provider output that does not match the requested image contract."""

    expected_width, expected_height = parse_image_size(size)
    image_format, width, height = inspect_image_payload(image.content)
    if image.extension == "jpeg":
        expected_format = "jpeg"
        accepted_formats = {"jpeg", "jpg"}
    else:
        expected_format = image.extension
        accepted_formats = {expected_format}
    if image_format not in accepted_formats:
        raise ValueError(
            f"{label} provider payload format mismatch: expected {expected_format}, "
            f"found {image_format}"
        )
    if (width, height) != (expected_width, expected_height):
        raise ValueError(
            f"{label} provider returned {width}x{height}; "
            f"expected exactly {expected_width}x{expected_height}"
        )
    return width, height


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
