"""Offline contracts for standard OpenAI GPT Image requests used by B2."""

import base64
import hashlib
import io
import json
from pathlib import Path
from urllib.error import URLError

import pytest
from PIL import Image

from ai_video_factory.providers import openai_batch_images as images


def _png(*, size: tuple[int, int] = (1280, 720)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format="PNG")
    return output.getvalue()


def _plan(*descriptions: str) -> dict[str, object]:
    return {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None},
        *({"beat_id": f"1{chr(ord('B') + index)}", "visual_type": "media_image",
           "description": description} for index, description in enumerate(descriptions)),
    ]}]}


def _avatar_plan(tmp_path: Path, description: str) -> tuple[dict, bytes]:
    avatar = _png(size=(3, 2))
    (tmp_path / "avatar.png").write_bytes(avatar)
    return {
        "avatar": {
            "file": "avatar.png",
            "sha256": hashlib.sha256(avatar).hexdigest(),
        },
        "blocks": [{"block_id": 1, "beats": [
            {"beat_id": "1A", "visual_type": "avatar", "description": None},
            {"beat_id": "1B", "visual_type": "media_image", "description": description},
        ]}],
    }, avatar


class FakeDirect:
    def __init__(self, *, unknown: bool = False) -> None:
        self.calls: list[dict] = []
        self.unknown = unknown

    def generate_png(self, **kwargs):
        self.calls.append(kwargs)
        if self.unknown:
            raise URLError("lost standard image POST")
        size = tuple(map(int, kwargs["size"].split("x")))
        content = _png(size=size)
        destination = kwargs["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "width": size[0],
            "height": size[1],
            "size": kwargs["size"],
            "output_format": "png",
            "usage": {"input_tokens": 4},
            "created": 8,
        }


def _opts(**overrides):
    return images.ImageOptions(**overrides)


def test_standard_requests_generate_one_image_per_job_and_cache_results(
    tmp_path: Path,
) -> None:
    direct = FakeDirect()
    plan = _plan("Original 1B  ", "Original 1C  ", "Original 1D  ")
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )

    assert result["status"] == "completed"
    assert result["openai_direct_count"] == 3
    assert [item["beat_id"] for item in result["items"]] == ["1B", "1C", "1D"]
    assert all(item["provider"] == "openai_direct" for item in result["items"])
    assert len(direct.calls) == 3
    assert not list((tmp_path / "images").rglob("*.batch.jsonl"))
    for call in direct.calls:
        assert call["prompt"].startswith("iphone 6 photo done by an elderly:  ")

    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    assert state["schema_version"] == "openai-image-v1"
    assert state["status"] == "completed"
    cached = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )
    assert cached["items"] == result["items"]
    assert len(direct.calls) == 3


def test_avatar_reference_uses_standard_edit_request_and_exact_prompt_suffix(
    tmp_path: Path,
) -> None:
    plan, avatar = _avatar_plan(tmp_path, "The avatar walking through the market")
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )

    assert result["items"][0]["provider"] == "openai_direct"
    assert len(direct.calls) == 1
    call = direct.calls[0]
    assert base64.b64decode(call["reference_image_url"].split(",", 1)[1]) == avatar
    assert call["prompt"].endswith(
        "set the reference image ONLY to preserve the avatar's general identity and appearance. "
        "Do NOT copy the reference image's lighting, camera quality, composition, background, "
        "color grading, framing, or professional photographic style.\n"
        "The photos should look genuinely amateur and casually taken by an elderly person with an "
        "iPhone 6"
    )


def test_avatar_preflight_blocks_missing_reference_before_paid_work(tmp_path: Path) -> None:
    plan, _avatar = _avatar_plan(tmp_path, "Avatar looking to camera")
    (tmp_path / "avatar.png").unlink()
    direct = FakeDirect()
    with pytest.raises(images.ImageGenerationError, match="avatar"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
        )
    assert direct.calls == []
    assert not (tmp_path / "images/manifest.json").exists()


def test_unknown_standard_post_is_not_repeated(tmp_path: Path) -> None:
    direct = FakeDirect(unknown=True)
    plan = _plan("Original 1B")
    with pytest.raises(images.ImageGenerationError, match="unknown"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
        )
    state_path = tmp_path / "images/block_1/1B.json"
    state = json.loads(state_path.read_text())
    assert state["status"] == "direct_submitting_unknown"
    with pytest.raises(images.ImageGenerationError, match="unknown"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
        )
    assert len(direct.calls) == 1


def test_nonstandard_legacy_state_is_rejected_before_standard_post(tmp_path: Path) -> None:
    state_path = tmp_path / "images/block_1/1B.json"
    images._atomic_json(state_path, {
        "schema_version": "openai-image-batch-v1",
        "status": "batch_submitted",
    })
    direct = FakeDirect()
    with pytest.raises(images.ImageGenerationError, match="non-standard"):
        images.generate_b2_images(
            tmp_path, _plan("Original 1B"), api_key=None,
            options=_opts(), direct_client=direct,
        )
    assert direct.calls == []


def test_numbered_sequence_uses_one_standard_request_for_grid(tmp_path: Path) -> None:
    plan = {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None},
        {
            "beat_id": "1B", "text": "First spoken line.",
            "visual_type": "media_image", "description": "1. First",
        },
        {
            "beat_id": "1C", "text": "Second spoken line.",
            "visual_type": "media_image", "description": "2. Second",
        },
    ]}]}
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )

    assert result["total_generation_jobs"] == 1
    assert result["openai_direct_count"] == 1
    assert len(direct.calls) == 1
    assert direct.calls[0]["size"] == "2560x1440"
    assert result["items"][0]["provider"] == "openai_direct"
    assert result["items"][0]["grid"]["columns"] == 2
    assert (
        "Voice over context:\n1B: First spoken line.\n1C: Second spoken line."
        in direct.calls[0]["prompt"]
    )
    assert direct.calls[0]["prompt"].endswith("Do not add text to the image.")
    assert not list((tmp_path / "images").rglob("*.batch.jsonl"))


def test_standard_generation_requires_key_when_no_client_is_injected(tmp_path: Path) -> None:
    with pytest.raises(images.ImageGenerationError, match="OPENAI_API_KEY"):
        images.generate_b2_images(
            tmp_path, _plan("Original 1B"), api_key=None, options=_opts(),
        )


@pytest.mark.parametrize("description, expected", [
    ("AVATAR at the harbor", True),
    ("near the avatar.", True),
    ("avatar-like silhouette", True),
    ("avatars in a crowd", False),
    ("avatarium", False),
])
def test_avatar_reference_requires_whole_word(description: str, expected: bool) -> None:
    assert images._avatar_requested(description) is expected


def test_standard_image_options_reject_old_or_incomplete_model_id() -> None:
    for model in ("gpt-image-2", "gpt-image-2.5"):
        with pytest.raises(ValueError, match="Unsupported official GPT Image model"):
            _opts(model_id=model)
