"""The standard official image endpoint remains the direct, paid batch fallback."""

import base64
import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.providers import openai_images as official


def _png(width: int = 1280, height: int = 720, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "model",
    ["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
)
def test_direct_client_uses_the_same_model_prompt_size_quality_and_single_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str,
) -> None:
    client = official.OpenAIImageClient("fake-key")
    payloads = []

    def fake_request(payload):
        payloads.append(payload)
        return {
            "created": 99,
            "data": [{"b64_json": base64.b64encode(_png()).decode()}],
            "usage": {"input_tokens": 11},
        }

    monkeypatch.setattr(client, "request_json", fake_request)
    destination = tmp_path / "result.png"
    result = client.generate_png(
        prompt="iphone 6 photo done by an elderly:  Test",
        model=model, destination=destination,
    )
    assert payloads == [{
        "model": model,
        "prompt": "iphone 6 photo done by an elderly:  Test",
        "size": "1280x720",
        "quality": "low",
        "output_format": "png",
        "n": 1,
    }]
    assert result["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert result["usage"] == {"input_tokens": 11}
    assert destination.read_bytes() == _png()


@pytest.mark.parametrize(("content", "message"), [
    (_png(1024, 1024), "dimensions"),
    (_png(fmt="JPEG"), "not a PNG"),
    (b"not a PNG", "invalid PNG"),
])
def test_direct_client_refuses_invalid_artifacts_without_writing(
    content: bytes, message: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = official.OpenAIImageClient("fake-key")
    monkeypatch.setattr(
        client, "request_json",
        lambda _request: {"data": [{"b64_json": base64.b64encode(content).decode()}]},
    )
    destination = tmp_path / "bad.png"
    with pytest.raises(official.OpenAIImageError, match=message):
        client.generate_png(
            prompt="Image", model="gpt-image-2.5-sunburst",
            destination=destination,
        )
    assert not destination.exists()
