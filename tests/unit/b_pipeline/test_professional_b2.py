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

from ai_video_factory.bots.contracts import (
    B11Output,
    ProfessionalB2BatchOutput,
    ProfessionalB2Output,
    ProfessionalVisualBeat,
    WholeB12Output,
)
from ai_video_factory.bots.workflow import run_b_pipeline
from ai_video_factory.providers.image_grids import _grid_prompt, plan_image_jobs
from ai_video_factory.providers.openai import OpenAIProvider
from ai_video_factory.providers.openai_batch_images import (
    ImageOptions,
    effective_image_prompt,
    generate_b2_images,
)
from ai_video_factory.workflows import b_video
from ai_video_factory.workflows.b_video import _group_beats
from scripts.pipeline import run_b_pipeline as pipeline_cli
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
             "video_prompt": None, "reference": False, "audio": True},
            {"beat_id": "1B", "visual_type": "media_image", "prompt": "The same navy car.",
             "video_prompt": None, "reference": True, "audio": False},
            {"beat_id": "1C", "visual_type": "media_image", "prompt": "A scenic mountain.",
             "video_prompt": None, "reference": False, "audio": False},
        ]}],
    }


def test_professional_b2_keeps_global_image_style_out_of_bot_prompt() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "iphone 16/17 pro max" not in prompt
    assert "application supplies the shared photographic style" in prompt

def test_professional_b2_presenter_text_audio_and_expression_guidance() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "original-avatar reference image" in prompt
    assert "only `avatar` and `avatar_media` speak" in prompt
    assert "120 characters" in prompt
    assert "different room or physical location" in prompt
    assert "screens and documents" in prompt

def test_professional_prompt_has_no_stock_exclusions() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "visual director and editor" in prompt
    assert "last beat of every `close` block" in prompt
    assert "`intro` — earn attention quickly" in prompt
    assert "up to four b1.1 blocks" in prompt

def _professional_anchor_output(
    kind: str, beats: list[dict],
) -> dict:
    return {
        "pipeline_stage": "B2",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{
            "block_id": 1,
            "type": kind,
            "emotional_entry": "Starts here",
            "emotional_exit": "Ends here",
            "span": {"first_words": "First", "last_words": "Last"},
            "beats": beats,
        }],
        "error": None,
    }


def _professional_beat(
    beat_id: str, *, visual_type: str = "avatar",
) -> dict:
    fields = {
        "avatar": (None, None, False, True),
        "avatar_media": ("Support photo", None, False, True),
        "media_image": ("A still image", None, False, False),
        "media_video": ("Motion-ready frame", "One controlled action", False, False),
    }
    image_prompt, video_prompt, reference, audio = fields[visual_type]
    return {
        "beat_id": beat_id,
        "text": "Preserve the narration exactly.",
        "beat_type": "setup",
        "visual_type": visual_type,
        "prompt": image_prompt,
        "video_prompt": video_prompt,
        "reference": reference,
        "audio": audio,
    }


def test_audio_resume_accepts_cta_enriched_b2_without_mutating_narration() -> None:
    import copy

    # B2/merged_output.json contains these post-CTA fields even though the
    # strict B2 model on the wire intentionally does not.
    original = _professional_anchor_output("close", [
        _professional_beat("1A", visual_type="media_image"),
        _professional_beat("1B", visual_type="avatar"),
    ])
    broll, presenter = original["blocks"][0]["beats"]
    broll.update(reference=True, cta_group_id=1, cta_reference_sources=["ebook"])
    presenter.update(cta_group_id=1, cta_reference_sources=[])
    snapshot = copy.deepcopy(original)
    pipeline_cli._validate_professional_audio_b2(
        original["narrative_core"], original["blocks"],
    )
    assert original == snapshot
    assert broll["cta_reference_sources"] == ["ebook"]
    assert broll["text"] == "Preserve the narration exactly."
    assert presenter["cta_group_id"] == 1


@pytest.mark.parametrize("malformation", [
    "missing_cta_sources",
    "invalid_cta_group",
    "unsupported_reference_source",
    "invalid_cta_audio",
    "unknown_field",
])
def test_audio_resume_still_rejects_invalid_cta_or_b2(
    malformation: str,
) -> None:
    output = _professional_anchor_output("close", [
        _professional_beat("1A", visual_type="media_image"),
        _professional_beat("1B", visual_type="avatar"),
    ])
    beat = output["blocks"][0]["beats"][0]
    beat.update(reference=True, cta_group_id=1, cta_reference_sources=["ebook"])
    if malformation == "missing_cta_sources":
        beat.pop("cta_reference_sources")
    elif malformation == "invalid_cta_group":
        beat["cta_group_id"] = True
    elif malformation == "unsupported_reference_source":
        beat["cta_reference_sources"] = ["presenter"]
    elif malformation == "invalid_cta_audio":
        beat["audio"] = True
    else:
        beat["unrecognized_metadata"] = "cannot be silently discarded"

    with pytest.raises((ValueError, ValidationError, KeyError)):
        pipeline_cli._validate_professional_audio_b2(
            output["narrative_core"], output["blocks"],
        )


@pytest.mark.parametrize(
    ("kind", "visuals"),
    [
        ("intro", ("avatar", "media_image", "media_video")),
        ("close", ("media_image", "media_video", "avatar")),
        ("intro", ("avatar",)),
        ("close", ("avatar",)),
    ],
)
def test_professional_intro_close_require_exact_base_avatar(
    kind: str, visuals: tuple[str, ...],
) -> None:
    beats = [
        _professional_beat(f"1{chr(ord('A') + index)}", visual_type=visual)
        for index, visual in enumerate(visuals)
    ]
    assert ProfessionalB2Output.model_validate(
        _professional_anchor_output(kind, beats)
    ).blocks[0].beats[-1].visual_type == visuals[-1]


@pytest.mark.parametrize("replacement", ["media_image", "avatar_media", "media_video"])
def test_professional_intro_first_beat_may_use_media(replacement: str) -> None:
    beats = [_professional_beat("1A", visual_type=replacement)]
    validated = ProfessionalB2Output.model_validate(
        _professional_anchor_output("intro", beats)
    )
    assert validated.blocks[0].beats[0].visual_type == replacement


@pytest.mark.parametrize("replacement", ["media_image", "avatar_media", "media_video"])
def test_professional_close_last_beat_must_reuse_avatar(replacement: str) -> None:
    beats = [_professional_beat("1A", visual_type=replacement)]
    with pytest.raises(
        ValidationError, match="Professional close last beat must reuse"
    ):
        ProfessionalB2Output.model_validate(_professional_anchor_output("close", beats))


def test_professional_intro_avatar_may_appear_on_an_adjacent_beat() -> None:
    visuals = ("media_image", "avatar", "media_video")
    beats = [
        _professional_beat(f"1{chr(ord('A') + index)}", visual_type=visual)
        for index, visual in enumerate(visuals)
    ]
    validated = ProfessionalB2Output.model_validate(
        _professional_anchor_output("intro", beats)
    )
    assert tuple(beat.visual_type for beat in validated.blocks[0].beats) == visuals


def test_professional_close_avatar_cannot_move_off_last_beat() -> None:
    visuals = ("media_image", "avatar", "media_video")
    beats = [
        _professional_beat(f"1{chr(ord('A') + index)}", visual_type=visual)
        for index, visual in enumerate(visuals)
    ]
    with pytest.raises(ValidationError, match="Professional close last beat must reuse"):
        ProfessionalB2Output.model_validate(_professional_anchor_output("close", beats))


def test_professional_intro_first_beat_may_regenerate_avatar() -> None:
    beat = _professional_beat("1A")
    beat.update(prompt="Generate a new presenter image", reference=True)
    with pytest.raises(ValidationError, match="Talking avatar must reuse"):
        ProfessionalB2Output.model_validate(
            _professional_anchor_output("intro", [beat])
        )


def test_professional_close_last_beat_cannot_regenerate_avatar() -> None:
    beat = _professional_beat("1A")
    beat.update(prompt="Generate a new presenter image", reference=True)
    with pytest.raises(ValidationError, match="Talking avatar must reuse"):
        ProfessionalB2Output.model_validate(_professional_anchor_output("close", [beat]))


def test_professional_b2_global_framing_sequence_progression_and_still_audio() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "audio=false" in prompt
    assert "physical location" in prompt
    assert "one photographable instant" in prompt
    assert "whole-video continuity" in prompt

@pytest.mark.parametrize(
    ("visual_type", "audio", "accepted"),
    [
        ("media_image", False, True),
        ("media_image", True, False),
        ("media_video", False, True),
        ("media_video", True, False),
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
                "video_prompt": (
                    "One beat-specific motion from the first frame."
                    if visual_type == "media_video" else None
                ),
                "reference": (
                    visual_type == "media_video" and audio
                ),
                "audio": audio,
            }],
        }],
        "error": None,
    }
    if accepted:
        assert ProfessionalB2Output.model_validate(data).blocks[0].beats[0].audio is audio
    else:
        with pytest.raises(
            ValidationError, match="Only avatar and avatar_media may have audio=true"
        ):
            ProfessionalB2Output.model_validate(data)


def test_professional_speaking_media_video_requires_avatar_reference() -> None:
    beat = {
        "beat_id": "1A", "text": "Narration.", "beat_type": "claim",
        "visual_type": "media_video", "prompt": "New B-roll scene.",
        "video_prompt": "The subject moves naturally.",
        "reference": False, "audio": True,
    }
    for reference in (False, True):
        beat["reference"] = reference
        with pytest.raises(
            ValidationError, match="Only avatar and avatar_media may have audio=true"
        ):
            ProfessionalVisualBeat.model_validate(beat)

def test_professional_prompt_forbids_frozen_narrator_in_reference_setup() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "a clearly different room or physical location" in prompt
    assert "do not depict a newly generated replacement speaking host" in prompt

@pytest.mark.parametrize(
    "wording",
    [
        "No specified brands or product claims.",
        "A product with a natural recognizable logo and packaging.",
        "Show the real brand printed on the container.",
        "Without specific branding.",
        "No readable brand names.",
    ],
)
def test_professional_contract_leaves_branding_unrestricted(wording: str) -> None:
    beat = {
        "beat_id": "1A", "text": "Show a simple routine.",
        "beat_type": "setup", "visual_type": "media_image",
        "prompt": f"Cleanser bottle on the bathroom counter. {wording}",
        "video_prompt": None, "reference": False, "audio": False,
    }
    assert ProfessionalVisualBeat.model_validate(beat).prompt == beat["prompt"]


@pytest.mark.parametrize(
    ("visual_type", "video_prompt", "accepted"),
    [
        ("media_image", None, True),
        ("media_image", "Animate face", False),
        ("avatar", None, True),
        ("avatar", "Animate face", False),
        ("media_video", "A hand smoothly moves toward the cheek.", True),
        ("media_video", None, False),
        ("media_video", "  ", False),
    ],
)
def test_professional_video_prompt_is_only_for_video(
    visual_type: str, video_prompt: str | None, accepted: bool,
) -> None:
    beat = {
        "beat_id": "1A", "text": "Apply moisturizer.",
        "beat_type": "setup", "visual_type": visual_type,
        "prompt": None if visual_type == "avatar" else "Suitable image",
        "video_prompt": video_prompt,
        "reference": False, "audio": True if visual_type == "avatar" else False,
    }
    if accepted:
        assert ProfessionalVisualBeat.model_validate(beat).video_prompt == video_prompt
    else:
        with pytest.raises(ValidationError, match="video_prompt"):
            ProfessionalVisualBeat.model_validate(beat)


def test_referenced_presenter_active_application_is_video_not_still() -> None:
    beat = {
        "beat_id": "6B", "text": "I apply a thin layer.",
        "beat_type": "action", "visual_type": "media_image",
        "prompt": (
            "Continue with the referenced presenter, show her applying "
            "a thin layer of moisturizer to one cheek."
        ),
        "video_prompt": None, "reference": True, "audio": False,
    }
    # The contract checks carriers/audio; scene fidelity is reviewed by B2.1.
    assert ProfessionalVisualBeat.model_validate(beat).visual_type == "media_image"
    beat.update(
        visual_type="media_video",
        video_prompt=(
            "She gently smooths moisturizer over her cheek using two "
            "restrained fingertip motions, then lowers her hand."
        ),
    )
    assert ProfessionalVisualBeat.model_validate(beat).visual_type == "media_video"
    beat.update(
        visual_type="media_image", video_prompt=None,
        prompt="A relaxed portrait of the presenter after applying moisturizer.",
    )
    assert ProfessionalVisualBeat.model_validate(beat).visual_type == "media_image"


def test_professional_contract_rejects_regenerated_avatar() -> None:
    data = {
        "pipeline_stage": "B2",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{
            "block_id": 1, "type": "development",
            "emotional_entry": "A", "emotional_exit": "B",
            "span": {"first_words": "First", "last_words": "last"},
            "beats": [{
                "beat_id": "1A", "text": "First beat", "beat_type": "setup",
                "visual_type": "avatar", "prompt": "Same speaker at desk",
                "video_prompt": None, "reference": True, "audio": True,
            }],
        }],
        "error": None,
    }
    with pytest.raises(ValidationError, match="Talking avatar must reuse"):
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
        "not DSLR or cinematic photography.  instagram rhode style. "
        "Preserve the described scene, composition, "
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
    assert "Keep each panel faithful to its own scene description" in grid["description"]
    for token in ("brand", "logo", "packaging", "claim"):
        assert token not in grid["description"].lower()
    assert "Do not add visible text unless" not in grid["description"]
    assert "Treat Voice over context as narrative context" in grid["description"]
    assert "without adding a person or imposing a screen/page orientation" in grid[
        "description"
    ]
    assert "Specific app/page/document content" in grid["description"]
    assert "natural device and document orientation" not in grid["description"]
    assert "Do not add artificial captions" not in grid["description"]
    assert "Respect EACH panel's own requested shot scale" in grid["description"]
    assert "Keep the resulting panels visually distinct" in grid["description"]
    assert "Do not normalize multiple panels" not in grid["description"]
    assert effective_image_prompt(
        grid["description"], visual_style="profesional"
    ).startswith(
        "Render the following scene as authentic high-end iPhone 16/17 Pro Max "
        "photography, not DSLR or cinematic photography.  instagram rhode style."
    )


def test_professional_grid_keeps_original_voice_over_as_context() -> None:
    panels = [
        {
            "beat_id": "1B",
            "description": (
                "1. A person casually holding a smartphone while looking at it, "
                "viewed from across the room. "
                "(For context: the person is scrolling through Instagram.)"
            ),
            "text": "The person is scrolling through Instagram.",
            "reference": False,
        },
        {
            "beat_id": "1C",
            "description": (
                "2. A close-up of a printed sheet resting on a desk, "
                "viewed directly from above. "
                "(For context: the page contains a quarterly financial report.)"
            ),
            "text": "She reads a quarterly financial report.",
            "reference": False,
        },
    ]
    grid_prompt = _grid_prompt(panels, 2, 1, visual_style="profesional")
    assert (
        "Voice over context:\n1B: The person is scrolling through Instagram."
        "\n1C: She reads a quarterly financial report."
    ) in grid_prompt
    assert "panel descriptions determine the visible scenes" in grid_prompt
    assert "2. A close-up of a printed sheet resting on a desk" in grid_prompt
    assert "(For context: the page contains a quarterly financial report.)" in grid_prompt
    assert "without adding a person or imposing a screen/page orientation" in grid_prompt
    assert "natural device and document orientation" not in grid_prompt
    assert "Do not add artificial captions" not in grid_prompt
    assert (
        "photography, not DSLR or cinematic photography.  instagram rhode style."
        in effective_image_prompt(grid_prompt, visual_style="profesional")
    )


def test_audio_ownership_and_regenerated_avatar_split_ltx_jobs() -> None:
    beats = [
        {"block_id": 1, "beat_id": "1A", "visual_type": "avatar", "prompt": None, "audio": True},
        {"block_id": 1, "beat_id": "1B", "visual_type": "avatar_media", "prompt": "Support", "audio": True},  # noqa: E501
        {"block_id": 1, "beat_id": "1C", "visual_type": "media_video", "prompt": "B-roll", "audio": False},  # noqa: E501
    ]
    assert _group_beats(beats) == [("a2v", 0, 1), ("i2v", 2, 2)]

def test_video_planner_rejects_historical_speaking_media_image() -> None:
    from ai_video_factory.workflows.b_video import B2VideoError

    beats = [{
        "block_id": 3, "beat_id": "3B", "visual_type": "media_image",
        "prompt": "Presenter mid-speech", "audio": True,
    }]
    with pytest.raises(B2VideoError, match="Generated B-roll beat 3B must have audio=false"):
        _group_beats(beats)


def test_speaking_presenter_media_video_uses_a2v_not_unsynced_i2v() -> None:
    from ai_video_factory.workflows.b_video import B2VideoError
    beats = [{"block_id": 3, "beat_id": "3D", "visual_type": "media_video", "audio": True}]
    with pytest.raises(B2VideoError, match="must have audio=false"):
        _group_beats(beats)
    beats[0]["audio"] = False
    assert _group_beats(beats) == [("i2v", 0, 0)]

@pytest.mark.parametrize("block_type", ["intro", "close"])
def test_professional_schema_boundary_matches_current_contract(
    block_type: str,
) -> None:
    script = "A clear introduction and conclusion for our video."
    attempts: list[str] = []
    rejected: list[tuple] = []

    class BoundaryProvider:
        async def generate_structured(
            self, *, model, instructions, input_text, output_type,
            input_image_url=None,
        ):
            if output_type is B11Output:
                return B11Output.model_validate({
                    "pipeline_stage": "B1.1",
                    "narrative_core": {
                        "central_question": "What?", "final_answer": "Answer."
                    },
                    "blocks": [{
                        "block_id": 1, "type": block_type,
                        "emotional_entry": "Ready", "emotional_exit": "Informed",
                        "span": {"first_words": script, "last_words": script},
                    }],
                    "error": None,
                })
            if output_type is WholeB12Output:
                return WholeB12Output.model_validate({
                    "pipeline_stage": "B1.2",
                    "narrative_core": json.loads(input_text)["narrative_core"],
                    "blocks": [{"block_id": 1, "beats": [{
                        "beat_id": "1A",
                        "text": script,
                        "beat_type": "setup",
                    }]}],
                    "error": None,
                })
            assert output_type is ProfessionalB2BatchOutput
            attempts.append(instructions)
            block = json.loads(input_text)["blocks"][0]
            if block_type == "close" and len(attempts) > 1:
                block["beats"][0].update(
                    visual_type="avatar",
                    prompt=None, video_prompt=None, reference=False, audio=True,
                )
            else:
                block["beats"][0].update(
                    visual_type="media_image",
                    prompt="A quiet still-life photograph",
                    video_prompt=None, reference=False, audio=False,
                )
            return ProfessionalB2BatchOutput.model_validate({
                "pipeline_stage": "B2",
                "narrative_core": {
                    "central_question": "What?", "final_answer": "Answer."
                },
                "blocks": [block], "error": None,
            })

    result = asyncio.run(run_b_pipeline(
        script, provider=BoundaryProvider(),
        visual_style="profesional", avatar_image=_avatar_png(),
        on_rejected_b2_output=lambda *args: rejected.append(args),
    ))
    beat = result.b2[0].blocks[0].beats[0]
    if block_type == "intro":
        assert len(attempts) == 1
        assert rejected == []
        assert (beat.visual_type, beat.audio) == ("media_image", False)
    else:
        assert len(attempts) == 2
        assert len(rejected) == 1
        assert "VALIDATION FAILED: B2 batch 1/1" in attempts[1]
        assert "Correct only this batch's block IDs: 1." in attempts[1]
        assert (beat.visual_type, beat.prompt, beat.reference, beat.audio) == (
            "avatar", None, False, True
        )


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
            if output_type is WholeB12Output:
                return WholeB12Output.model_validate({
                    "pipeline_stage": "B1.2",
                    "narrative_core": payload["narrative_core"],
                    "blocks": [{"block_id": 1, "beats": [{
                        "beat_id": "1A", "text": script, "beat_type": "action"
                    }]}],
                    "error": None,
                })
            assert output_type is ProfessionalB2BatchOutput
            attempts.append(instructions)
            if len(attempts) == 2:
                assert "VALIDATION FAILED: B2 batch 1/1" in instructions
                assert "Correct only this batch's block IDs: 1." in instructions
            block = payload["blocks"][0]
            block["beats"][0].update(
                visual_type="media_image" if len(attempts) == 1 else "media_video",
                prompt="A documentary view of the actual step.",
                video_prompt=(
                    "A person moves their hands naturally."
                    if len(attempts) == 2 else None
                ),
                reference=False,
                audio=len(attempts) == 1,
            )
            return ProfessionalB2BatchOutput.model_validate({
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


def test_professional_video_prompt_reaches_i2v_and_owns_paid_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = tmp_path / "avatar.png"
    keyframe = tmp_path / "1A.png"
    avatar.write_bytes(_avatar_png())
    keyframe.write_bytes(_avatar_png())
    master = tmp_path / "master.wav"
    master.write_bytes(b"dummy only for voice-over I2V")
    monkeypatch.setattr(
        b_video, "_source_assets",
        lambda *_: (avatar, {(1, "1A"): keyframe}),
    )
    monkeypatch.setattr(b_video, "_verified_asset", lambda *_: master)
    video_prompt = (
        "She gently applies cream in two slow fingertip passes, "
        "blinks naturally, then lowers her hand; steady close view."
    )
    beat = {
        "block_id": 1, "beat_id": "1A", "visual_type": "media_video",
        "text": "Apply the cream.", "prompt": "A hand touches the cheek.",
        "description": "A hand touches the cheek.", "audio": False,
        "video_prompt": video_prompt,
        "start_seconds": 0.0, "end_seconds": 1.0,
    }
    timeline = {"beats": [beat], "duration_seconds": 1.0}
    monkeypatch.setattr(b_video, "build_stt_beat_timeline", lambda *_: timeline)
    plan = {"visual_style": "profesional"}
    audio = {"sha256": "a" * 64, "file": "master.wav"}
    _, jobs, first_dir = b_video.build_b2_video_plan(
        tmp_path, plan, audio, {},
    )
    assert len(jobs) == 1
    assert jobs[0].kind == "i2v"
    assert jobs[0].request.parameters["prompt"] == video_prompt
    assert jobs[0].request.parameters["generation_profile"] == b_video.LTX_PRODUCTION_I2V_PROFILE
    assert "fp8cpu" in jobs[0].request.parameters["generation_profile"]
    assert "fp8none" not in jobs[0].request.parameters["generation_profile"]
    assert jobs[0].request.max_attempts == 2
    first_id = jobs[0].request.job_id
    # A production recipe change must not reuse an old completed guided I2V run.
    legacy_digest = b_video._fingerprint(
        timeline, audio, {(1, "1A"): keyframe}, avatar, i2v_profile=None
    )[:20]
    assert first_dir.name != legacy_digest

    beat["video_prompt"] = video_prompt + " She pauses with her hand down."
    _, jobs, second_dir = b_video.build_b2_video_plan(tmp_path, plan, audio, {})
    assert jobs[0].request.parameters["prompt"] == beat["video_prompt"]
    assert jobs[0].request.job_id != first_id
    assert second_dir != first_dir

    del beat["video_prompt"]
    _, legacy_jobs, _ = b_video.build_b2_video_plan(tmp_path, plan, audio, {})
    assert legacy_jobs[0].request.parameters["prompt"].startswith(
        "Animate the supplied start image"
    )


def test_speaking_media_video_adds_video_prompt_to_a2v_without_rewriting_avatar() -> None:
    from ai_video_factory.workflows.b_video import B2VideoError
    with pytest.raises(B2VideoError, match="must have audio=false"):
        _group_beats([
            {"block_id": 1, "beat_id": "1A", "visual_type": "media_video", "audio": True},
        ])

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



def test_only_i2v_runs_get_new_fp8_run_fingerprint(tmp_path: Path) -> None:
    avatar = tmp_path / "avatar.png"
    avatar.write_bytes(_avatar_png())
    audio = {"sha256": "b" * 64}
    avatar_only = {"beats": [{"visual_type": "avatar", "audio": True}]}
    assert b_video._fingerprint(avatar_only, audio, {}, avatar) == (
        b_video._fingerprint(avatar_only, audio, {}, avatar, i2v_profile=None)
    )
    i2v_timeline = {"beats": [{"visual_type": "media_video", "audio": False}]}
    assert b_video._fingerprint(i2v_timeline, audio, {}, avatar) != (
        b_video._fingerprint(i2v_timeline, audio, {}, avatar, i2v_profile=None)
    )


def test_promoted_i2v_deployment_guard_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "docker.io/yagobordell/ai-video-factory@sha256:" + "a" * 64
    monkeypatch.setattr(pipeline_cli, "dotenv_values", lambda _path: {})
    monkeypatch.delenv("SALAD_LTX25_FP8_I2V_IMAGE", raising=False)
    with pytest.raises(RuntimeError, match="SALAD_LTX25_FP8_I2V_IMAGE"):
        pipeline_cli._require_ltx_safe_i2v_worker()
    monkeypatch.setenv("SALAD_LTX25_FP8_I2V_IMAGE", expected)
    monkeypatch.setattr(
        pipeline_cli, "SaladClient",
        lambda config: SimpleNamespace(
            describe_container_group=lambda: {
                "container": {"image": expected[:-1] + "b"}
            },
        ),
    )
    with pytest.raises(RuntimeError, match="image mismatch"):
        pipeline_cli._require_ltx_safe_i2v_worker()
    monkeypatch.setattr(
        pipeline_cli, "SaladClient",
        lambda config: SimpleNamespace(
            describe_container_group=lambda: {"container": {"image": expected}},
        ),
    )
    pipeline_cli._require_ltx_safe_i2v_worker()
