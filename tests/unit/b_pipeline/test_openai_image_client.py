"""The standard official image endpoint used by B2."""

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
    ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
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


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (_png(1024, 1024), "dimensions"),
        (_png(fmt="JPEG"), "not a PNG"),
        (b"not a PNG", "invalid PNG"),
    ],
    ids=["wrong_dimensions", "jpeg", "garbage"],
)
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


@pytest.mark.parametrize("old_model", ["gpt-image-2", "gpt-image-2.5"])
def test_standard_client_rejects_old_or_incomplete_model_id(
    old_model: str, tmp_path: Path,
) -> None:
    client = official.OpenAIImageClient("fake-key")
    with pytest.raises(ValueError, match="supported GPT Image model"):
        client.generate_png(
            prompt="Image", model=old_model, destination=tmp_path / "bad.png",
        )
    assert not (tmp_path / "bad.png").exists()


def test_direct_edit_uses_the_selected_avatar_without_changing_model_or_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = official.OpenAIImageClient("fake-key")
    payloads = []
    monkeypatch.setattr(
        client, "request_json",
        lambda _payload: pytest.fail("Referenced avatar must use image edits"),
    )

    def fake_edit(payload):
        payloads.append(payload)
        return {
            "created": 99,
            "data": [{"b64_json": base64.b64encode(_png()).decode()}],
        }

    monkeypatch.setattr(client, "request_edit_json", fake_edit)
    reference = "data:image/png;base64," + base64.b64encode(_png(3, 2)).decode()
    result = client.generate_png(
        prompt="Avatar in a scene", model="gpt-image-2.5-flare",
        destination=tmp_path / "edited.png", reference_image_url=reference,
    )
    assert len(payloads) == 1
    assert payloads[0]["images"] == [{"image_url": reference}]
    assert payloads[0]["model"] == "gpt-image-2.5-flare"
    assert payloads[0]["size"] == "1280x720"
    assert payloads[0]["quality"] == "low"
    assert payloads[0]["prompt"] == "Avatar in a scene"
    assert result["sha256"] == hashlib.sha256(_png()).hexdigest()


@pytest.mark.parametrize("model", ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"])
@pytest.mark.parametrize("with_reference", [False, True])
def test_medium_quality_grid_reaches_the_official_generation_or_edit_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str,
    with_reference: bool,
) -> None:
    client = official.OpenAIImageClient("fake-key")
    calls = []
    response = {
        "data": [{"b64_json": base64.b64encode(_png(2560, 1440)).decode()}],
    }
    monkeypatch.setattr(
        client, "request_json",
        lambda payload: calls.append(("generate", payload)) or response,
    )
    monkeypatch.setattr(
        client, "request_edit_json",
        lambda payload: calls.append(("edit", payload)) or response,
    )
    reference_url = "data:image/png;base64,AAAA" if with_reference else None

    result = client.generate_png(
        prompt="One coherent 2x2 grid", model=model, size="2560x1440",
        quality="medium", destination=tmp_path / "grid.png",
        reference_image_url=reference_url,
    )

    assert len(calls) == 1
    endpoint, payload = calls[0]
    assert endpoint == ("edit" if with_reference else "generate")
    assert payload["model"] == model
    assert payload["size"] == "2560x1440"
    assert payload["quality"] == "medium"
    assert payload["output_format"] == "png"
    assert ("images" in payload) is with_reference
    assert result["quality"] == "medium"
