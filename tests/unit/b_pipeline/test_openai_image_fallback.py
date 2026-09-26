"""Offline 30-minute AI33 timeout -> official GPT Image fallback contracts."""

import base64
import hashlib
import io
import json
import time
from pathlib import Path
from urllib.error import URLError

import pytest
from PIL import Image

from ai_video_factory.providers import ai33_images as ai33
from ai_video_factory.providers import openai_images as official


def _png(width: int = 1280, height: int = 720, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format=fmt)
    return buffer.getvalue()


def _job() -> dict[str, object]:
    return {
        "block_id": 1,
        "beat_id": "1B",
        "visual_type": "media_image",
        "description": "  Producto en una mesa  ",
    }


def _plan() -> dict[str, object]:
    return {"blocks": [{"block_id": 1, "beats": [_job()]}]}


def _pending_state(output: Path, options: ai33.AI33ImageOptions) -> Path:
    job = _job()
    state_path = output / "images/block_1/1B.json"
    ai33._atomic_json(
        state_path,
        {
            **job,
            **options.public(),
            "fingerprint": ai33._fingerprint(job, options),
            "status": "submitted",
            "task_id": "existing-paid-ai33-task",
            "submitted_at_unix": time.time() - options.poll_timeout_seconds - 2,
        },
    )
    return state_path


class PendingAI33:
    def __init__(self, *, finished: bool = False) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.finished = finished

    def request_json(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method != "GET":
            raise AssertionError("A known AI33 task must never be submitted twice")
        if not self.finished:
            return {"id": "existing-paid-ai33-task", "status": "doing", "progress": 80}
        return {
            "id": "existing-paid-ai33-task",
            "status": "done",
            "progress": 100,
            "credit_cost": 882,
            "metadata": {
                "providerCreditCost": 191,
                "result_images": [{
                    "imageUrl": "https://example.test/image.png",
                    "mimeType": "image/png",
                    "width": 1280,
                    "height": 720,
                }],
            },
        }

    def download_png(self, url: str, destination: Path):
        assert url == "https://example.test/image.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = _png()
        destination.write_bytes(content)
        return hashlib.sha256(content).hexdigest(), 1280, 720


class FakeOfficial:
    def __init__(self, *, uncertain: bool = False) -> None:
        self.calls = []
        self.uncertain = uncertain

    def generate_png(self, **kwargs):
        self.calls.append(kwargs)
        if self.uncertain:
            raise URLError("lost response after official paid POST")
        destination = kwargs["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = _png()
        destination.write_bytes(data)
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": 1280,
            "height": 720,
            "model": kwargs["model"],
            "size": kwargs["size"],
            "quality": kwargs["quality"],
            "output_format": kwargs["output_format"],
            "usage": {"input_tokens": 11},
            "created": 123,
        }


def test_official_client_sends_exact_model_prompt_low_minimum_16_9(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = official.OpenAIImageClient("fake-openai-key")
    sent = []

    def fake_request(payload):
        sent.append(payload)
        return {
            "created": 123,
            "data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}],
            "usage": {"input_tokens": 11},
        }

    monkeypatch.setattr(client, "request_json", fake_request)
    output = tmp_path / "image.png"
    result = client.generate_png(
        prompt="  Producto en una mesa  ",
        model="gpt-image-2.5-sunburst",
        destination=output,
    )
    assert sent == [{
        "model": "gpt-image-2.5-sunburst",
        "prompt": "  Producto en una mesa  ",
        "size": "1280x720",
        "quality": "low",
        "output_format": "png",
        "n": 1,
    }]
    assert output.read_bytes() == _png()
    assert result["size"] == "1280x720"
    assert result["width"] == 1280 and result["height"] == 720
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["usage"] == {"input_tokens": 11}
    assert "b64_json" not in result


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (_png(1024, 1024), "dimensions"),
        (_png(fmt="JPEG"), "not a PNG"),
        (b"not an image", "invalid PNG"),
    ],
)
def test_official_client_rejects_bad_image_without_writing(
    content: bytes, message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = official.OpenAIImageClient("fake-openai-key")
    monkeypatch.setattr(
        client,
        "request_json",
        lambda _payload: {"data": [{"b64_json": base64.b64encode(content).decode()}]},
    )
    dest = tmp_path / "image.png"
    with pytest.raises(official.OpenAIImageError, match=message):
        client.generate_png(prompt="Descripción", model="gpt-image-2.5-flare", destination=dest)
    assert not dest.exists()


def test_known_ai33_timeout_uses_same_model_prompt_and_persists_fallback(
    tmp_path: Path,
) -> None:
    options = ai33.AI33ImageOptions(model_id="gpt-image-2.5-sunburst")
    state_path = _pending_state(tmp_path, options)
    ai33_client = PendingAI33()
    openai_client = FakeOfficial()
    manifest = ai33.generate_b2_images(
        tmp_path,
        _plan(),
        api_key=None,
        options=options,
        client=ai33_client,
        fallback_enabled=True,
        openai_client=openai_client,
    )

    assert len(ai33_client.calls) == 1
    assert ai33_client.calls[0][0] == "GET"
    assert len(openai_client.calls) == 1
    assert openai_client.calls[0]["prompt"] == (
        "iphone 6 photo done by an elderly:    Producto en una mesa  "
    )
    assert openai_client.calls[0]["model"] == "gpt-image-2.5-sunburst"
    assert openai_client.calls[0]["size"] == "1280x720"
    assert openai_client.calls[0]["quality"] == "low"
    assert openai_client.calls[0]["output_format"] == "png"
    item = manifest["items"][0]
    assert manifest["ai33_count"] == 0
    assert manifest["openai_fallback_count"] == 1
    assert item["provider"] == "openai_official_fallback"
    assert item["task_id"] == "existing-paid-ai33-task"
    assert item["ai33_timeout_seconds"] == 1800
    assert item["credit_cost"] is None
    assert item["openai_usage"] == {"input_tokens": 11}
    assert (tmp_path / item["file"]).is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["submitted_at_unix"] < time.time() - 1800
    assert state["fallback_request"]["size"] == "1280x720"
    assert state["fallback_request"]["prompt_sha256"] == hashlib.sha256(
        ai33.effective_image_prompt(_job()["description"]).encode("utf-8")
    ).hexdigest()

    second = ai33.generate_b2_images(
        tmp_path,
        _plan(),
        api_key=None,
        options=options,
        client=ai33_client,
        fallback_enabled=True,
        openai_client=openai_client,
    )
    assert second["items"] == manifest["items"]
    assert len(openai_client.calls) == 1
    assert len(ai33_client.calls) == 1


def test_expired_ai33_task_that_finished_at_final_check_avoids_fallback(
    tmp_path: Path,
) -> None:
    options = ai33.AI33ImageOptions()
    _pending_state(tmp_path, options)
    ai33_client = PendingAI33(finished=True)
    official_client = FakeOfficial()
    result = ai33.generate_b2_images(
        tmp_path,
        _plan(),
        api_key=None,
        options=options,
        client=ai33_client,
        fallback_enabled=True,
        openai_client=official_client,
    )
    assert result["ai33_count"] == 1
    assert result["openai_fallback_count"] == 0
    assert result["items"][0]["provider"] == "ai33"
    assert result["items"][0]["credit_cost"] == 882
    assert official_client.calls == []


def test_missing_official_key_preserves_known_timed_out_task_for_resume(
    tmp_path: Path,
) -> None:
    options = ai33.AI33ImageOptions()
    state_path = _pending_state(tmp_path, options)
    ai33_client = PendingAI33()
    with pytest.raises(ai33.AI33ImageError, match="OPENAI_API_KEY is missing"):
        ai33.generate_b2_images(
            tmp_path,
            _plan(),
            api_key=None,
            options=options,
            client=ai33_client,
            fallback_enabled=True,
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["task_id"] == "existing-paid-ai33-task"
    assert state["status"] == "ai33_timed_out"

    official_client = FakeOfficial()
    restored = ai33.generate_b2_images(
        tmp_path,
        _plan(),
        api_key=None,
        options=options,
        client=ai33_client,
        fallback_enabled=True,
        openai_client=official_client,
    )
    assert restored["openai_fallback_count"] == 1
    assert len(ai33_client.calls) == 1
    assert len(official_client.calls) == 1


def test_official_uncertain_response_is_not_resubmitted(
    tmp_path: Path,
) -> None:
    options = ai33.AI33ImageOptions()
    state_path = _pending_state(tmp_path, options)
    ai33_client = PendingAI33()
    official_client = FakeOfficial(uncertain=True)
    with pytest.raises(ai33.AI33ImageError, match="outcome"):
        ai33.generate_b2_images(
            tmp_path,
            _plan(),
            api_key=None,
            options=options,
            client=ai33_client,
            fallback_enabled=True,
            openai_client=official_client,
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "openai_submitting_unknown"
    assert state["task_id"] == "existing-paid-ai33-task"
    assert ai33.has_pending_image_tasks(tmp_path)
    with pytest.raises(ai33.AI33ImageError, match="cannot be retried safely"):
        ai33.generate_b2_images(
            tmp_path,
            _plan(),
            api_key=None,
            options=options,
            client=ai33_client,
            fallback_enabled=True,
            openai_client=official_client,
        )
    assert len(official_client.calls) == 1
    assert len(ai33_client.calls) == 1


def test_ai33_unknown_submission_never_triggers_official_fallback(
    tmp_path: Path,
) -> None:
    options = ai33.AI33ImageOptions()
    job = _job()
    state_path = tmp_path / "images/block_1/1B.json"
    ai33._atomic_json(
        state_path,
        {**job, "fingerprint": ai33._fingerprint(job, options),
         "status": "submitting_unknown"},
    )
    ai33_client = PendingAI33()
    official_client = FakeOfficial()
    with pytest.raises(ai33.AI33ImageError, match="cannot be retried safely"):
        ai33.generate_b2_images(
            tmp_path,
            _plan(),
            api_key=None,
            options=options,
            client=ai33_client,
            fallback_enabled=True,
            openai_client=official_client,
        )
    assert not ai33_client.calls and not official_client.calls
