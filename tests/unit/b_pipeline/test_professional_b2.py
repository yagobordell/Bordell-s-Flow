"""Offline regression tests for the opt-in professional B2 visual contract."""

import asyncio
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from ai_video_factory.bots.contracts import ProfessionalB2Output
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.openai import OpenAIProvider
from ai_video_factory.providers.openai_batch_images import (
    ImageOptions,
    effective_image_prompt,
    generate_b2_images,
)
from ai_video_factory.workflows.b_video import _group_beats
from scripts.pipeline.run_b_pipeline import select_visual_style


def _avatar_png() -> bytes:
    result = io.BytesIO()
    Image.new("RGB", (24, 16)).save(result, "PNG")
    return result.getvalue()


def _plan(avatar: bytes) -> dict:
    return {
        "visual_style": "profesional",
        "avatar": {"file": "avatar.png", "sha256": hashlib.sha256(avatar).hexdigest()},
        "blocks": [{"block_id": 1, "beats": [
            {"beat_id": "1A", "visual_type": "avatar", "prompt": None,
             "reference": False, "audio": True},
            {"beat_id": "1B", "visual_type": "media_image", "prompt": "The same navy car.",
             "reference": True, "audio": False},
            {"beat_id": "1C", "visual_type": "media_image", "prompt": "A scenic mountain.",
             "reference": False, "audio": False},
        ]}],
    }


def test_professional_contract_accepts_regenerated_avatar() -> None:
    data = {
        "pipeline_stage": "B2",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{"block_id": 1, "type": "intro",
                    "emotional_entry": "A", "emotional_exit": "B",
                    "span": {"first_words": "First", "last_words": "last"},
                    "beats": [{"beat_id": "1A", "text": "First beat", "beat_type": "setup",
                               "visual_type": "avatar", "prompt": "Same speaker at desk",
                               "reference": True, "audio": True}]}],
        "error": None,
    }
    assert ProfessionalB2Output.model_validate(data).blocks[0].beats[0].reference
    data["blocks"][0]["beats"][0]["reference"] = False
    with pytest.raises(ValidationError, match="reference"):
        ProfessionalB2Output.model_validate(data)


def test_professional_image_requests_send_exact_prompts_and_explicit_reference(
    tmp_path: Path,
) -> None:
    avatar = _avatar_png()
    (tmp_path / "avatar.png").write_bytes(avatar)
    calls = []

    class Direct:
        def generate_png(self, **kwargs):
            calls.append(kwargs)
            size = tuple(map(int, kwargs["size"].split("x")))
            dest = kwargs["destination"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            result = io.BytesIO()
            Image.new("RGB", size).save(result, "PNG")
            raw = result.getvalue()
            dest.write_bytes(raw)
            return {"size": kwargs["size"], "width": size[0], "height": size[1],
                    "sha256": hashlib.sha256(raw).hexdigest(), "usage": None,
                    "created": 0}

    manifest = generate_b2_images(
        tmp_path, _plan(avatar), api_key=None, options=ImageOptions(),
        direct_client=Direct(),
    )
    assert manifest["status"] == "completed"
    assert len(calls) == 2
    assert {call["prompt"] for call in calls} == {
        "The same navy car.", "A scenic mountain."
    }
    assert sum("reference_image_url" in call for call in calls) == 1
    assert effective_image_prompt(
        "Full cinematic prompt", avatar_reference=True, visual_style="profesional"
    ) == "Full cinematic prompt"


def test_professional_grid_never_adds_amateur_header_and_scopes_reference() -> None:
    avatar = _avatar_png()
    plan = _plan(avatar)
    plan["blocks"][0]["beats"][1]["prompt"] = "1. Same car on driveway."
    plan["blocks"][0]["beats"][2]["prompt"] = "2. Mountain vista."
    described = [
        {"block_id": 1, "beat_id": b["beat_id"], "visual_type": b["visual_type"],
         "description": b["prompt"], "reference": b["reference"],
         "visual_style": "profesional"}
        for b in plan["blocks"][0]["beats"] if b["prompt"]
    ]
    jobs = plan_image_jobs(plan, described)
    grid = next(job for job in jobs if job.get("grid"))
    assert "iPhone 6" not in grid["description"]
    assert "iphone 6" not in grid["description"]
    assert "only for panels 1" in grid["description"].lower()
    assert grid["visual_style"] == "profesional"


def test_audio_ownership_and_regenerated_avatar_split_ltx_jobs() -> None:
    beats = [
        {"block_id": 1, "beat_id": "1A", "visual_type": "avatar", "prompt": None,
         "audio": True},
        {"block_id": 1, "beat_id": "1B", "visual_type": "avatar_media",
         "prompt": "Support image", "audio": True},
        {"block_id": 1, "beat_id": "1C", "visual_type": "avatar",
         "prompt": "Regenerated speaker", "audio": True},
        {"block_id": 1, "beat_id": "1D", "visual_type": "media_image",
         "prompt": "Speaking presenter", "audio": True},
        {"block_id": 1, "beat_id": "1E", "visual_type": "media_video",
         "prompt": "Walking presenter", "audio": False},
    ]
    assert _group_beats(beats) == [
        ("a2v", 0, 1), ("a2v", 2, 2), ("a2v", 3, 3), ("i2v", 4, 4)
    ]


def test_style_selector_defaults_to_legacy_for_unattended_execution() -> None:
    assert select_visual_style(None, interactive=False) == "casero"
    assert select_visual_style("profesional", interactive=False) == "profesional"


def test_professional_responses_request_attaches_avatar_image() -> None:
    seen = []

    class Responses:
        async def parse(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(output_parsed="parsed", usage=None)

    provider = OpenAIProvider(
        client=SimpleNamespace(responses=Responses()), api_key="fake",
        service_tier="default",
    )
    result = asyncio.run(provider.generate_structured(
        model="test", instructions="B2", input_text='{"blocks":[]}',
        input_image_url="data:image/png;base64,AAAA", output_type=str,
    ))
    assert result == "parsed"
    content = seen[0]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": '{"blocks":[]}'}
    assert content[1] == {
        "type": "input_image", "image_url": "data:image/png;base64,AAAA"
    }
