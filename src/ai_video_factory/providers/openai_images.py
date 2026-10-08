"""Official OpenAI GPT Image client for standard image generation.

The caller owns paid-request idempotency and persists its state before calling
generate_png. Never automatically retry a POST with an unknown outcome.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import time
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

_OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
_OPENAI_EDITS_URL = "https://api.openai.com/v1/images/edits"
_MAX_PNG_BYTES = 40 * 1024 * 1024
_DEFAULT_SIZE = "1280x720"
_AVATAR_MEDIA_SIZE = "768x864"
_GRID_SIZES = {"2560x1440", "3840x1440", "3840x2160"}
# 1920x1080 is not divisible by 16. Request native 1920x1088 from
# GPT Image and deterministically center-crop only the eight extra rows.
_PRO_VIDEO_SIZE = "1920x1088"
_AVATAR_MEDIA_GRID_SIZES = {"1536x1728", "2304x1728", "2304x2592"}
_SUPPORTED_SIZES = {
    _DEFAULT_SIZE,
    _AVATAR_MEDIA_SIZE,
    _PRO_VIDEO_SIZE,
    *_GRID_SIZES,
    *_AVATAR_MEDIA_GRID_SIZES,
}


class OpenAIImageError(RuntimeError):
    """The official image API returned unusable data or an invalid image."""


class OpenAIImageRequestRejected(OpenAIImageError):
    """The API returned a definite HTTP rejection before any image artifact."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.api_message = message
        super().__init__(f"OpenAI image API HTTP {status_code}: {message}")


class OpenAIImageClient:
    """Generate one PNG through the standard official OpenAI image API."""

    def __init__(self, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for official image generation")
        self._api_key = api_key

    @staticmethod
    def _retry_after_seconds(exc: HTTPError, attempt: int) -> float:
        header = exc.headers.get("Retry-After") if exc.headers is not None else None
        if isinstance(header, str):
            try:
                value = float(header.strip())
            except ValueError:
                value = -1.0
            if math.isfinite(value) and value >= 0:
                return min(value, 60.0)
        return min(float(2 ** attempt), 30.0)

    def _request_json(
        self, url: str, payload: dict[str, object]
    ) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(4):
            try:
                with urlopen(request, timeout=180) as response:
                    result = json.load(response)
                break
            except HTTPError as exc:
                # A 429 is a definite rejection before an image artifact exists,
                # so retrying it after Retry-After cannot duplicate paid output.
                if exc.code == 429 and attempt < 3:
                    time.sleep(self._retry_after_seconds(exc, attempt))
                    continue
                # Other 4xx responses remain explicit, non-retryable rejections.
                if 400 <= exc.code < 500:
                    try:
                        raw = exc.read().decode("utf-8", errors="replace")
                        payload_error = json.loads(raw)
                        detail = (
                            payload_error.get("error")
                            if isinstance(payload_error, dict) else None
                        )
                        message = (
                            detail.get("message") if isinstance(detail, dict) else None
                        )
                        if not isinstance(message, str) or not message.strip():
                            message = raw.strip() or exc.reason or "request rejected"
                    except (OSError, ValueError, TypeError):
                        message = str(exc.reason or "request rejected")
                    raise OpenAIImageRequestRejected(exc.code, message) from exc
                raise
        else:  # pragma: no cover - loop always exits by success or exception.
            raise OpenAIImageError("OpenAI image retry loop exhausted")
        if not isinstance(result, dict):
            raise OpenAIImageError("Official OpenAI image API returned non-object JSON")
        return result

    def request_json(self, payload: dict[str, object]) -> dict[str, Any]:
        return self._request_json(_OPENAI_IMAGES_URL, payload)

    def request_edit_json(self, payload: dict[str, object]) -> dict[str, Any]:
        return self._request_json(_OPENAI_EDITS_URL, payload)

    def generate_png(
        self,
        *,
        prompt: str,
        model: str,
        destination: Path,
        size: str = _DEFAULT_SIZE,
        quality: str = "low",
        output_format: str = "png",
        reference_image_url: str | None = None,
        reference_image_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        if model not in {
            "gpt-image-2.5-flare",
            "gpt-image-2.5-sunburst",
        }:
            raise ValueError("Official image generation must use a supported GPT Image model")
        if size not in _SUPPORTED_SIZES:
            raise ValueError("Official image generation requires a supported image or grid size")
        if quality not in {"low", "medium"} or output_format != "png":
            raise ValueError("Official image generation requires low/medium quality and PNG")
        payload: dict[str, object] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "n": 1,
        }
        if reference_image_url is not None and reference_image_urls is not None:
            raise ValueError("Choose either one or several ordered image references")
        if reference_image_urls is not None:
            if not reference_image_urls or not all(
                isinstance(url, str) and url.startswith("data:image/png;base64,")
                for url in reference_image_urls
            ):
                raise ValueError("Multi-reference edits require ordered PNG data URLs")
            payload["images"] = [{"image_url": url} for url in reference_image_urls]
            response = self.request_edit_json(payload)
        elif reference_image_url is not None:
            payload["images"] = [{"image_url": reference_image_url}]
            response = self.request_edit_json(payload)
        else:
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
        expected_width, expected_height = map(int, size.split("x"))
        if (width, height) != (expected_width, expected_height):
            raise OpenAIImageError(
                f"Official OpenAI image dimensions are {width}x{height}, not {size}"
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
