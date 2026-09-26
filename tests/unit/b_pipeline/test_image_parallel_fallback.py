"""Offline parallel B2 image jobs and immediate per-beat official fallback contracts."""

import hashlib
import io
import json
import threading
from pathlib import Path
from urllib.error import URLError

import pytest
from PIL import Image

from ai_video_factory.providers import ai33_images as images


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1280, 720)).save(buffer, format="PNG")
    return buffer.getvalue()


def _plan(*beats: str) -> dict[str, object]:
    return {
        "blocks": [{
            "block_id": 1,
            "beats": [
                {"beat_id": beat, "visual_type": "media_image",
                 "description": f"image {beat}"}
                for beat in beats
            ],
        }],
    }


class AI33:
    def __init__(self, *, failures: dict[str, str] | None = None) -> None:
        self.failures = failures or {}
        self.submissions: list[str] = []
        self.lock = threading.Lock()
        self.barrier: threading.Barrier | None = None
        self.active = 0
        self.peak = 0

    def request_json(self, method, path, payload=None):
        if path == "/v1i/task/price":
            beat = payload["prompt"].rsplit(" ", 1)[-1]
            if self.failures.get(beat) == "pricing":
                raise URLError("AI33 pricing is unavailable")
            if self.barrier is not None:
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                try:
                    self.barrier.wait(timeout=5)
                finally:
                    with self.lock:
                        self.active -= 1
            return {"success": True, "credits": 10}
        if path == "/v1i/task/generate-image":
            beat = payload["prompt"].rsplit(" ", 1)[-1]
            with self.lock:
                self.submissions.append(beat)
            failure = self.failures.get(beat)
            if failure == "unknown":
                raise URLError("lost paid POST response")
            if failure == "rejected":
                return {"success": False, "code": "rejected"}
            return {"success": True, "task_id": beat}
        assert method == "GET", (method, path)
        beat = path.rsplit("/", 1)[-1]
        if self.failures.get(beat) == "polling":
            return {"status": "failed", "id": beat}
        data = [] if self.failures.get(beat) == "download" else [{
            "imageUrl": f"https://example.test/{beat}.png",
            "mimeType": "image/png", "width": 1280, "height": 720,
        }]
        return {"status": "done", "id": beat, "credit_cost": 8,
                "metadata": {"result_images": data, "providerCreditCost": 3}}

    def download_png(self, url: str, destination: Path):
        content = _png()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return hashlib.sha256(content).hexdigest(), 1280, 720


class Official:
    def __init__(self, *, uncertain: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.uncertain = uncertain
        self.lock = threading.Lock()

    def generate_png(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
        if self.uncertain:
            raise URLError("lost official paid POST response")
        content = _png()
        destination = kwargs["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "width": 1280, "height": 720,
            "size": "1280x720", "output_format": "png", "usage": None,
            "created": 1,
        }


def test_images_actually_start_in_parallel_and_manifest_stays_in_b2_order(
    tmp_path: Path,
) -> None:
    ai33 = AI33()
    ai33.barrier = threading.Barrier(2)
    result = images.generate_b2_images(
        tmp_path, _plan("1B", "1C"), api_key=None,
        options=images.AI33ImageOptions(), client=ai33, max_parallel_images=2,
    )
    assert ai33.peak == 2
    assert result["status"] == "completed"
    assert [item["beat_id"] for item in result["items"]] == ["1B", "1C"]
    assert result["ai33_count"] == 2
    assert (tmp_path / "images/manifest.json").is_file()


@pytest.mark.parametrize("failure,stage", [
    ("pricing", "pricing"),
    ("rejected", "submission_rejected"),
    ("polling", "polling"),
    ("download", "download"),
])
def test_one_failed_beat_falls_back_immediately_without_blocking_other_images(
    tmp_path: Path, failure: str, stage: str,
) -> None:
    ai33 = AI33(failures={"1B": failure})
    official = Official()
    manifest = images.generate_b2_images(
        tmp_path, _plan("1B", "1C"), api_key=None,
        options=images.AI33ImageOptions(), client=ai33,
        fallback_enabled=True, openai_client=official,
        max_parallel_images=2,
    )
    assert manifest["status"] == "completed"
    assert manifest["ai33_count"] == 1
    assert manifest["openai_fallback_count"] == 1
    first, second = manifest["items"]
    assert first["beat_id"] == "1B"
    assert first["provider"] == "openai_official_fallback"
    assert first["fallback_reason"] == "ai33_failure"
    assert first["ai33_failure_stage"] == stage
    assert second["beat_id"] == "1C" and second["provider"] == "ai33"
    assert len(official.calls) == 1
    assert official.calls[0]["prompt"] == images.effective_image_prompt("image 1B")
    assert len(ai33.submissions) == (1 if failure == "pricing" else 2)
    assert len(ai33.submissions) == len(set(ai33.submissions))
    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    assert state["status"] == "completed"


def test_unknown_ai33_post_cannot_trigger_fallback_and_other_beat_is_saved(
    tmp_path: Path,
) -> None:
    ai33 = AI33(failures={"1B": "unknown"})
    official = Official()
    kwargs = dict(
        api_key=None, options=images.AI33ImageOptions(), client=ai33,
        fallback_enabled=True, openai_client=official, max_parallel_images=2,
    )
    with pytest.raises(images.AI33ImageError, match="1B"):
        images.generate_b2_images(tmp_path, _plan("1B", "1C"), **kwargs)
    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    manifest = json.loads((tmp_path / "images/manifest.json").read_text())
    assert state["status"] == "submitting_unknown"
    assert manifest["status"] == "incomplete"
    assert [item["beat_id"] for item in manifest["items"]] == ["1C"]
    assert official.calls == []
    with pytest.raises(images.AI33ImageError, match="1B"):
        images.generate_b2_images(tmp_path, _plan("1B", "1C"), **kwargs)
    assert sorted(ai33.submissions) == ["1B", "1C"]
    assert official.calls == []


def test_missing_official_key_resumes_confirmed_failure_without_ai33_resubmit(
    tmp_path: Path,
) -> None:
    ai33 = AI33(failures={"1B": "polling"})
    kwargs = dict(
        api_key=None, options=images.AI33ImageOptions(), client=ai33,
        fallback_enabled=True, max_parallel_images=2,
    )
    with pytest.raises(images.AI33ImageError, match="OPENAI_API_KEY is missing"):
        images.generate_b2_images(tmp_path, _plan("1B", "1C"), **kwargs)
    before = list(ai33.submissions)
    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    assert state["status"] == "ai33_failed"
    official = Official()
    manifest = images.generate_b2_images(
        tmp_path, _plan("1B", "1C"), **kwargs, openai_client=official,
    )
    assert manifest["status"] == "completed"
    assert len(official.calls) == 1
    assert ai33.submissions == before


def test_uncertain_official_post_never_repeats_on_resume(tmp_path: Path) -> None:
    ai33 = AI33(failures={"1B": "polling"})
    official = Official(uncertain=True)
    kwargs = dict(
        api_key=None, options=images.AI33ImageOptions(), client=ai33,
        fallback_enabled=True, openai_client=official, max_parallel_images=2,
    )
    with pytest.raises(images.AI33ImageError, match="1B"):
        images.generate_b2_images(tmp_path, _plan("1B", "1C"), **kwargs)
    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    assert state["status"] == "openai_submitting_unknown"
    with pytest.raises(images.AI33ImageError, match="1B"):
        images.generate_b2_images(tmp_path, _plan("1B", "1C"), **kwargs)
    assert len(official.calls) == 1
    assert sorted(ai33.submissions) == ["1B", "1C"]
