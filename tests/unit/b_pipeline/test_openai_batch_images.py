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
        assert call["quality"] == "low"
    assert all(item["quality"] == "low" for item in result["items"])

    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    assert state["schema_version"] == "openai-image-v1"
    assert state["status"] == "completed"
    cached = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )
    assert cached["items"] == result["items"]
    assert len(direct.calls) == 3



@pytest.mark.parametrize(("scene", "expected"), [
    ("A table with a glass of water", "low"),
    ("A laptop. (For context: the page contains a report.)", "medium"),
    ("A laptop. ( For context: the page contains a report.)", "medium"),
    ("A laptop. (For context : the page contains a report.)", "medium"),
    ("A laptop. ( For context : the page contains a report.)", "medium"),
    ("A laptop. (   fOr  CoNtExT  : text", "medium"),
    ("A laptop. (\n For\tcontext: message", "medium"),
    ("A table discussing for context with no parenthesis", "low"),
    ("A page about the word contextualization", "low"),
    ("A file labeled (for contexts: topic)", "low"),
])
@pytest.mark.parametrize("visual_style", ["casero", "profesional"])
def test_for_context_alone_promotes_individual_image_quality(
    tmp_path: Path, scene: str, expected: str, visual_style: str,
) -> None:
    plan = _plan(scene)
    if visual_style == "profesional":
        plan["visual_style"] = "profesional"
        for beat in plan["blocks"][0]["beats"]:
            beat["reference"] = False
            beat["audio"] = beat["visual_type"] == "avatar"
            beat["prompt"] = beat.pop("description")
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )
    assert direct.calls[0]["quality"] == expected
    assert result["items"][0]["quality"] == expected
    state = json.loads((tmp_path / "images/block_1/1B.json").read_text())
    assert state["quality"] == expected
    assert state["artifact"]["quality"] == expected
    images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )
    assert len(direct.calls) == 1


def test_context_quality_change_refuses_unpaid_silent_regeneration(
    tmp_path: Path,
) -> None:
    direct = FakeDirect()
    first = _plan("A screen. (For context: quarterly results.)")
    images.generate_b2_images(
        tmp_path, first, api_key=None, options=_opts(), direct_client=direct,
    )
    with pytest.raises(images.ImageGenerationError, match="request differs"):
        images.generate_b2_images(
            tmp_path, _plan("A screen."), api_key=None,
            options=_opts(), direct_client=direct,
        )
    assert len(direct.calls) == 1


def test_avatar_media_uses_exact_split_aspect_without_crop(tmp_path: Path) -> None:
    plan = {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None},
        {
            "beat_id": "1B", "visual_type": "avatar_media",
            "description": "A closed red book on a wooden table",
        },
    ]}]}
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )

    assert len(direct.calls) == 1
    assert direct.calls[0]["size"] == "768x864"
    assert result["items"][0]["size"] == "768x864"
    assert (result["items"][0]["width"], result["items"][0]["height"]) == (768, 864)


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
    assert direct.calls[0]["quality"] == "medium"
    assert result["quality"] == "low"
    assert result["grid_quality"] == "medium"
    assert result["items"][0]["quality"] == "medium"
    saved_grid = json.loads(
        (tmp_path / "images/grids/block_1_1B.json").read_text(encoding="utf-8")
    )
    assert saved_grid["quality"] == "medium"
    assert saved_grid["artifact"]["quality"] == "medium"
    assert result["items"][0]["provider"] == "openai_direct"
    assert result["items"][0]["grid"]["columns"] == 2
    assert (
        "Voice over context:\n1B: First spoken line.\n1C: Second spoken line."
        in direct.calls[0]["prompt"]
    )
    assert direct.calls[0]["prompt"].endswith("Do not add text to the image.")
    assert not list((tmp_path / "images").rglob("*.batch.jsonl"))


def test_numbered_avatar_media_sequence_uses_8_9_grid_cells(tmp_path: Path) -> None:
    plan = {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None},
        {
            "beat_id": "1B", "text": "First support image.",
            "visual_type": "avatar_media", "description": "1. Red notebook on desk",
        },
        {
            "beat_id": "1C", "text": "Second support image.",
            "visual_type": "avatar_media", "description": "2. Same notebook opened",
        },
    ]}]}
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )

    assert result["total_generation_jobs"] == 1
    assert len(direct.calls) == 1
    assert direct.calls[0]["size"] == "1536x1728"
    assert "2x2 grid 8:9" in direct.calls[0]["prompt"]
    grid = result["items"][0]["grid"]
    assert (grid["panel_width"], grid["panel_height"]) == (768, 864)


def test_mixed_numbered_sequence_avoids_wrong_aspect_grid(tmp_path: Path) -> None:
    plan = {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None},
        {
            "beat_id": "1B", "text": "Presenter support.",
            "visual_type": "avatar_media", "description": "1. Red notebook on desk",
        },
        {
            "beat_id": "1C", "text": "Full-screen continuation.",
            "visual_type": "media_image", "description": "2. Same notebook opened",
        },
    ]}]}
    direct = FakeDirect()
    result = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=_opts(), direct_client=direct,
    )

    assert result["grid_count"] == 0
    assert result["total_generation_jobs"] == 2
    assert {
        call["destination"].stem: call["size"] for call in direct.calls
    } == {"1B": "768x864", "1C": "1280x720"}
    assert [item["beat_id"] for item in result["items"]] == ["1B", "1C"]


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


@pytest.mark.parametrize("model", ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"])
def test_mixed_grid_and_individual_jobs_use_separate_qualities(
    tmp_path: Path, model: str,
) -> None:
    # A 2x2 grid consumes the two nearest fillers, leaving one standalone beat.
    plan = _plan(
        "1. First moment", "2. Second moment", "Filler one", "Filler two",
        "A separate final image",
    )
    direct = FakeDirect()
    options = _opts(model_id=model)
    manifest = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=options, direct_client=direct,
    )

    assert manifest["status"] == "completed"
    assert manifest["grid_count"] == 1
    assert manifest["total_generation_jobs"] == 2
    assert {(call["size"], call["quality"]) for call in direct.calls} == {
        ("2560x1440", "medium"), ("1280x720", "low"),
    }
    assert {(item["size"], item["quality"]) for item in manifest["items"]} == {
        ("2560x1440", "medium"), ("1280x720", "low"),
    }
    assert len(direct.calls) == 2

    # Completed paid work remains cached with the exact quality in its fingerprint.
    again = images.generate_b2_images(
        tmp_path, plan, api_key=None, options=options, direct_client=direct,
    )
    assert again["items"] == manifest["items"]
    assert len(direct.calls) == 2


def test_saved_low_quality_grid_cannot_be_silently_resubmitted_as_medium(
    tmp_path: Path,
) -> None:
    plan = _plan("1. First", "2. Second")
    options = _opts()
    described = images.described_beats(plan)
    job = images.plan_image_jobs(plan, described)[0]
    assert job["grid"] is not None
    old_request = options.request(job["description"], size=job["size"])
    old_fingerprint = hashlib.sha256(
        json.dumps({
            "beat_id": job["beat_id"], "block_id": job["block_id"],
            **old_request,
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    state_path = tmp_path / "images/grids/block_1_1B.json"
    images._atomic_json(state_path, {
        "schema_version": "openai-image-v1", "fingerprint": old_fingerprint,
        "status": "completed",
    })
    direct = FakeDirect()
    with pytest.raises(images.ImageGenerationError, match="Image request differs"):
        images.generate_b2_images(
            tmp_path, plan, api_key=None, options=options, direct_client=direct,
        )
    assert direct.calls == []
    assert not (tmp_path / "images/manifest.json").exists()
