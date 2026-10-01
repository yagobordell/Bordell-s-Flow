"""Offline regression tests for the opt-in professional B2 visual contract."""

import asyncio
import hashlib
import io
import json
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from ai_video_factory.bots.contracts import B11Output, B12Output, ProfessionalB2Output
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.openai import OpenAIProvider
from ai_video_factory.providers.openai_batch_images import (
    ImageOptions,
    effective_image_prompt,
    generate_b2_images,
)
from ai_video_factory.bots.workflow import run_b_pipeline
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


def test_professional_b2_keeps_global_image_style_out_of_bot_prompt() -> None:
    instructions = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
    )
    assert "iPhone 16/17 Pro Max" not in instructions
    assert "application injects the canonical image style" in instructions.lower()


def test_professional_b2_presenter_text_audio_and_expression_guidance() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
    )
    # Casting preference must not become a media/presenter visual_type quota.
    assert "Visual format and human casting are independent decisions" in prompt
    assert "prefer using the actual presenter" in prompt
    assert "story characters keep their own identity" in prompt
    # The same photo can preserve identity without freezing expression and gaze.
    assert "Never rely on the reference photo's fixed smile" in prompt
    assert "If the face is not visible" in prompt
    # Voice sync is allowed for simple speaking-compatible demonstrations, not
    # misrepresented as complex action animation from the A2V model.
    assert "Visible-presenter speech preference" in prompt
    assert "A2V does not guarantee detailed" in prompt
    assert "use audio=false and observational media_video" in prompt
    # Scene-native packaging and signs are no longer banned indiscriminately.
    assert "Do not systematically end prompts with" in prompt
    assert "do not invent product claims" in prompt


def test_professional_b2_global_framing_sequence_progression_and_still_audio() -> None:
    instructions = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
    )
    assert "Global shot composition and camera-angle freedom" in instructions
    assert "For EVERY generated beat throughout the video" in instructions
    assert "not only in numbered" in instructions
    assert "slightly zoomed-in COMPOSITION" in instructions
    assert "Downstream\nKen Burns handles still-image motion" in instructions
    assert "Shot progression is ESPECIALLY important within continuous sequences" in instructions
    assert "near-duplicate images" in instructions
    assert "For media_image, audio MUST always be false" in instructions
    assert "generated synchronized speech" in instructions


@pytest.mark.parametrize(
    ("visual_type", "audio", "accepted"),
    [
        ("media_image", False, True),
        ("media_image", True, False),
        ("media_video", False, True),
        ("media_video", True, True),
    ],
)
def test_professional_contract_requires_speaking_generated_shots_to_be_video(
    visual_type: str, audio: bool, accepted: bool,
) -> None:
    data = {
        "pipeline_stage": "B2",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{
            "block_id": 1, "type": "development",
            "emotional_entry": "A", "emotional_exit": "B",
            "span": {"first_words": "First", "last_words": "last"},
            "beats": [{
                "beat_id": "1A", "text": "First beat", "beat_type": "setup",
                "visual_type": visual_type, "prompt": "Relevant scene",
                "reference": False, "audio": audio,
            }],
        }],
        "error": None,
    }
    if accepted:
        assert ProfessionalB2Output.model_validate(data).blocks[0].beats[0].audio is audio
    else:
        with pytest.raises(ValidationError, match="media_image must have audio=false"):
            ProfessionalB2Output.model_validate(data)


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
    style = (
        "Render the following scene as authentic high-end iPhone 16/17 Pro Max photography, "
        "not DSLR or cinematic photography. Preserve the described scene, composition, "
        "relationships, lighting, and mood, realistic skin texture, natural facial imperfections, "
        "authentic everyday environment, documentary feel, not polished, not cinematic, "
        "not luxury editorial."
    )
    assert {call["prompt"] for call in calls} == {
        f"{style} The same navy car.", f"{style} A scenic mountain."
    }
    assert sum("reference_image_url" in call for call in calls) == 1
    assert effective_image_prompt(
        "Full scene prompt", avatar_reference=True, visual_style="profesional"
    ) == f"{style} Full scene prompt"
    assert effective_image_prompt(
        "2. Same person in the same locker room.", visual_style="profesional"
    ) == f"2. {style} Same person in the same locker room."


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
    assert "iPhone 16/17 Pro Max" not in grid["description"]
    assert "only for panels 1" in grid["description"].lower()
    assert grid["visual_style"] == "profesional"
    assert "Allow natural scene-native labels, packaging and signage" in grid["description"]
    assert "Do not add visible text unless" not in grid["description"]
    assert "Do not add artificial captions, overlays or watermarks" in grid["description"]
    assert "Respect EACH panel's own requested shot scale" in grid["description"]
    assert "Do not normalize multiple panels" in grid["description"]
    assert effective_image_prompt(
        grid["description"], visual_style="profesional"
    ).startswith("Render the following scene as authentic high-end iPhone 16/17 Pro Max")


def test_audio_ownership_and_regenerated_avatar_split_ltx_jobs() -> None:
    beats = [
        {"block_id": 1, "beat_id": "1A", "visual_type": "avatar", "prompt": None,
         "audio": True},
        {"block_id": 1, "beat_id": "1B", "visual_type": "avatar_media",
         "prompt": "Support image", "audio": True},
        {"block_id": 1, "beat_id": "1C", "visual_type": "avatar",
         "prompt": "Regenerated speaker", "audio": True},
        {"block_id": 1, "beat_id": "1D", "visual_type": "media_video",
         "prompt": "Speaking presenter", "audio": True},
        {"block_id": 1, "beat_id": "1E", "visual_type": "media_video",
         "prompt": "Walking presenter", "audio": False},
    ]
    assert _group_beats(beats) == [
        ("a2v", 0, 1), ("a2v", 2, 2), ("a2v", 3, 3), ("i2v", 4, 4)
    ]


def test_video_planner_rejects_historical_speaking_media_image() -> None:
    from ai_video_factory.workflows.b_video import B2VideoError

    beats = [{
        "block_id": 3, "beat_id": "3B", "visual_type": "media_image",
        "prompt": "Presenter mid-speech", "audio": True,
    }]
    with pytest.raises(B2VideoError, match="Speaking media_image beat 3B is invalid"):
        _group_beats(beats)


def test_speaking_presenter_media_video_uses_a2v_not_unsynced_i2v() -> None:
    beats = [
        {"block_id": 3, "beat_id": "3D", "visual_type": "media_video",
         "prompt": "Presenter with visible mouth lightly pressing toner on cheeks.",
         "reference": True, "audio": True},
        {"block_id": 3, "beat_id": "3E", "visual_type": "media_video",
         "prompt": "Hands-only close view of toner being spread.",
         "reference": False, "audio": False},
    ]
    assert _group_beats(beats) == [
        ("a2v", 0, 0), ("i2v", 1, 1)
    ]


def test_professional_schema_retry_recovers_from_speaking_still() -> None:
    script = "Apply a small amount of foundation with a gentle hand."
    attempts = []
    rejected = []

    class InvalidThenFixed:
        async def generate_structured(
            self, *, model, instructions, input_text, output_type, input_image_url=None
        ):
            payload = json.loads(input_text)
            if output_type is B11Output:
                return B11Output.model_validate({
                    "pipeline_stage": "B1.1",
                    "narrative_core": {
                        "central_question": "How?", "final_answer": "Apply gently."
                    },
                    "blocks": [{
                        "block_id": 1, "type": "development",
                        "emotional_entry": "Curious", "emotional_exit": "Informed",
                        "span": {"first_words": script, "last_words": script},
                    }],
                    "error": None,
                })
            if output_type is B12Output:
                return B12Output.model_validate({
                    "pipeline_stage": "B1.2", "block_id": 1,
                    "beats": [{
                        "beat_id": "1A", "text": script, "beat_type": "action"
                    }],
                    "error": None,
                })
            assert output_type is ProfessionalB2Output
            attempts.append(instructions)
            if len(attempts) == 2:
                assert "media_image ALWAYS requires audio=false" in instructions
                assert "nonempty prompt" in instructions
            block = payload["blocks"][0]
            block["beats"][0].update(
                visual_type="media_image" if len(attempts) == 1 else "media_video",
                prompt="Referenced speaker with visible mouth.",
                reference=True,
                audio=True,
            )
            return ProfessionalB2Output.model_validate({
                "pipeline_stage": "B2", "narrative_core": payload["narrative_core"],
                "blocks": [block], "error": None,
            })

    result = asyncio.run(run_b_pipeline(
        script, provider=InvalidThenFixed(), visual_style="profesional",
        avatar_image=_avatar_png(),
        on_rejected_b2_output=lambda *args: rejected.append(args),
    ))
    assert len(attempts) == 2
    assert len(rejected) == 1
    assert rejected[0][3]["unparsed_response"] is True
    assert result.b2[0].blocks[0].beats[0].visual_type == "media_video"


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
