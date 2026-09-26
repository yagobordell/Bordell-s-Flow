"""Official OpenAI GPT Image fallback for AI33 tasks that exceed 30 minutes.

The caller owns paid-request idempotency and persists its state before calling
generate_png. Never automatically retry a POST with an unknown outcome.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from PIL import Image

_OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
_MAX_PNG_BYTES = 30 * 1024 * 1024
_FALLBACK_SIZE = "1280x720"


class OpenAIImageError(RuntimeError):
    """The official image API returned unusable data or an invalid image."""


class OpenAIImageClient:
    """Generate one PNG with the same GPT Image model and low quality as AI33."""

    def __init__(self, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for the official image fallback")
        self._api_key = api_key

    def request_json(self, payload: dict[str, object]) -> dict[str, Any]:
        request = Request(
            _OPENAI_IMAGES_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=180) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise OpenAIImageError("Official OpenAI image API returned non-object JSON")
        return result

    def generate_png(
        self,
        *,
        prompt: str,
        model: str,
        destination: Path,
        size: str = _FALLBACK_SIZE,
        quality: str = "low",
        output_format: str = "png",
    ) -> dict[str, Any]:
        if model not in {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}:
            raise ValueError("Fallback must use the same verified GPT Image model")
        if size != _FALLBACK_SIZE or quality != "low" or output_format != "png":
            raise ValueError("Official fallback requires 1280x720, low, PNG")
        payload: dict[str, object] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "n": 1,
        }
        response = self.request_json(payload)
        images = response.get("data")
        if not isinstance(images, list) or not images or not isinstance(images[0], dict):
            raise OpenAIImageError("Official OpenAI image API did not return image data")
        encoded = images[0].get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            raise OpenAIImageError("Official OpenAI image API did not return b64_json")
        if len(encoded) > (_MAX_PNG_BYTES * 4 // 3) + 4:
            raise OpenAIImageError("Official OpenAI image response exceeds 30 MiB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise OpenAIImageError("Official OpenAI image response has invalid base64") from exc
        if len(data) > _MAX_PNG_BYTES:
            raise OpenAIImageError("Official OpenAI PNG exceeds 30 MiB")
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format != "PNG":
                    raise OpenAIImageError("Official OpenAI image is not a PNG")
                width, height = image.size
                image.verify()
        except (OSError, ValueError) as exc:
            raise OpenAIImageError("Official OpenAI image contains invalid PNG bytes") from exc
        if (width, height) != (1280, 720):
            raise OpenAIImageError(
                f"Official OpenAI image dimensions are {width}x{height}, not 1280x720"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        if destination.is_symlink() or temporary.is_symlink():
            raise OpenAIImageError("Refusing to replace a linked image output")
        try:
            temporary.write_bytes(data)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        usage = response.get("usage")
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": width,
            "height": height,
            "model": model,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "usage": usage if isinstance(usage, dict) else None,
            "created": response.get("created"),
        }
