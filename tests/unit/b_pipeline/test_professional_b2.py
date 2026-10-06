"""Offline regression tests for the opt-in professional B2 visual contract."""

import asyncio
import hashlib
import io
import json
import wave
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from ai_video_factory.bots.contracts import (
    B11Output,
    B12Output,
    ProfessionalB2Output,
    ProfessionalVisualBeat,
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
    # Voice sync is restricted to the narrator's stable reference setup.
    assert "NARRATOR SETUP RULE" in prompt
    assert "audio=true is reserved for the supplied avatar" in prompt
    assert "physical location/placement" in prompt
    assert "stand up from a seated setup" in prompt
    assert "paper, phone or product" in prompt
    assert "NARRATOR-IN-SETUP NO-FREEZE RULE" in prompt
    assert "A2V does not guarantee detailed" in prompt
    # Branding choices are deliberately outside the B2 policy.
    for token in ("branding", "brands", "logos", "packaging"):
        assert token not in prompt.lower()
    assert "STRONG PRESENTER ACTION DEFAULT" in prompt
    assert "Video animation prompt (video_prompt)" in prompt
    assert "When audio=false, video_prompt is sent DIRECTLY to I2V" in prompt
    assert "When audio=true, video_prompt is instead sent to the A2V" in prompt


def test_professional_b2_video_only_reliability_rules_are_explicit() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
    )
    assert "MEDIA_VIDEO GENERATION RELIABILITY RULE — VIDEO ONLY" in prompt
    assert (
        "They do NOT ban these subjects, objects or completed states from\n"
        "media_image in general"
    ) in prompt
    assert "writing on paper" in prompt
    assert "typing or entering visible numbers" in prompt
    assert "scratching a card" in prompt
    assert "cutting\nwith scissors or a knife in close-up" in prompt
    assert "threading, fastening/buckling, inserting a\ncard" in prompt
    assert "opening a small zipper or clasp" in prompt
    assert "manipulating a coin over a precise target\narea" in prompt
    assert "scratching a card so a code appears" in prompt
    assert "opening something so letters appear" in prompt
    assert "writing a word that did not previously exist" in prompt
    assert "turning an object specifically so\nexact content is expected to appear" in prompt
    assert "A forbidden motion is NOT a reason by itself to choose media_image" in prompt
    assert "KEEP media_video" in prompt
    assert "media_image is NOT an allowed fallback or\nsubstitution" in prompt
    assert "re-stage only the fragile action as a nearby safe motion" in prompt
    assert "showing an already completed\nstate while reacting to it" in prompt
    assert "Never move a listed forbidden action into video_prompt" in prompt
    assert "media_image showing the relevant before, after or completed state" not in prompt


def test_professional_prompt_requires_boundary_anchors_and_no_stock_exclusions() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
    )
    assert "The FIRST beat of every type=intro block MUST" in prompt
    assert "The LAST beat of every type=close block MUST" in prompt
    assert "prompt=null, video_prompt=null, reference=false, audio=true" in prompt
    assert "skip discretionary steps 3-12" in prompt
    assert "Write every image prompt as an affirmative description" in prompt
    assert "omit\nnegative prompt clauses, preventive exclusions" in prompt
    # Keep these assertions synchronized with main's current screen/document
    # context section. B2 prompt policy itself is out of scope for this A2V PR.
    assert "Screens and documents: context belongs outside the visible scene" in prompt
    assert "Choose the visible scene, subject, shot size and camera viewpoint" in prompt
    assert "(For context:" in prompt
    assert "Apply the same separation in video_prompt" in prompt
    assert "Preserve the same relevant" in prompt
    assert "specific app, page or document content only as parenthetical context" in prompt
    # Avoid replacing one brand rule with another, including in this correction.
    for word in ("brands", "branding", "logos", "packaging"):
        assert word not in prompt.lower()


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


@pytest.mark.parametrize(
    ("kind", "replacement"),
    [
        ("intro", "media_image"),
        ("intro", "avatar_media"),
        ("intro", "media_video"),
        ("close", "media_image"),
        ("close", "avatar_media"),
        ("close", "media_video"),
    ],
)
def test_professional_boundary_cannot_be_replaced_by_media(
    kind: str, replacement: str,
) -> None:
    beats = [_professional_beat("1A", visual_type=replacement)]
    with pytest.raises(
        ValidationError, match=f"Professional {kind} .* beat must reuse"
    ):
        ProfessionalB2Output.model_validate(_professional_anchor_output(kind, beats))


@pytest.mark.parametrize(
    ("kind", "visuals"),
    [
        ("intro", ("media_image", "avatar", "media_video")),
        ("close", ("media_image", "avatar", "media_video")),
    ],
)
def test_professional_anchor_cannot_be_moved_to_an_adjacent_beat(
    kind: str, visuals: tuple[str, ...],
) -> None:
    beats = [
        _professional_beat(f"1{chr(ord('A') + index)}", visual_type=visual)
        for index, visual in enumerate(visuals)
    ]
    with pytest.raises(ValidationError, match=f"Professional {kind} .* beat"):
        ProfessionalB2Output.model_validate(_professional_anchor_output(kind, beats))


@pytest.mark.parametrize("kind", ["intro", "close"])
def test_professional_boundary_cannot_regenerate_avatar(kind: str) -> None:
    beat = _professional_beat("1A")
    beat.update(prompt="Generate a new presenter image", reference=True)
    with pytest.raises(
        ValidationError, match=f"Professional {kind} .* beat must reuse"
    ):
        ProfessionalB2Output.model_validate(_professional_anchor_output(kind, [beat]))


def test_professional_b2_continuity_can_be_anchored_by_an_object() -> None:
    instructions = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
    )
    assert "An OBJECT ALONE can be the continuity anchor" in instructions
    assert "presenter+object to object-only close-up" in instructions
    assert "A presenter holding a particular gift card" in instructions
    assert "detail of the reverse side of that SAME gift card" in instructions
    assert "presence of the presenter" in instructions
    assert "reference=true/reference=false" in instructions
    assert "same underlying visual reality" in instructions
    assert "object-led sequence" in instructions
    assert "including object-only anchors" in instructions


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
    assert "NARRATOR-IN-SETUP NO-FREEZE RULE" in instructions


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
        with pytest.raises(ValidationError, match="media_image must have audio=false"):
            ProfessionalB2Output.model_validate(data)


def test_professional_speaking_media_video_requires_avatar_reference() -> None:
    beat = {
        "beat_id": "1A",
        "text": "I am still speaking from my usual setup.",
        "beat_type": "claim",
        "visual_type": "media_video",
        "prompt": "The narrator remains seated in the reference setup.",
        "video_prompt": "She speaks naturally with one small hand gesture.",
        "reference": False,
        "audio": True,
    }
    with pytest.raises(ValidationError, match="Speaking media_video must use"):
        ProfessionalVisualBeat.model_validate(beat)

    beat["reference"] = True
    assert ProfessionalVisualBeat.model_validate(beat).audio is True


def test_professional_prompt_forbids_frozen_narrator_in_reference_setup() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_professional.md")
        .read_text(encoding="utf-8")
    )
    assert (
        "NEVER show the narrator sitting/placed in the preserved narration setup "
        "as a frozen still"
    ) in prompt
    assert "Never use media_image or media_video with audio=false" in prompt
    assert "physical placement/posture class" in prompt
    assert "different camera angle, crop or shot size is allowed" in prompt


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
        "prompt": "Suitable image", "video_prompt": video_prompt,
        "reference": True, "audio": True if visual_type == "avatar" else False,
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
    with pytest.raises(ValidationError, match="active practical step"):
        ProfessionalVisualBeat.model_validate(beat)
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


def test_professional_contract_accepts_regenerated_avatar() -> None:
    data = {
        "pipeline_stage": "B2",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{"block_id": 1, "type": "development",
                    "emotional_entry": "A", "emotional_exit": "B",
                    "span": {"first_words": "First", "last_words": "last"},
                    "beats": [{"beat_id": "1A", "text": "First beat", "beat_type": "setup",
                               "visual_type": "avatar", "prompt": "Same speaker at desk",
                               "video_prompt": None, "reference": True, "audio": True}]}],
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


@pytest.mark.parametrize("block_type", ["intro", "close"])
def test_professional_schema_retry_restores_mandatory_boundary(
    block_type: str,
) -> None:
    script = "A clear introduction and conclusion for our video."
    attempts: list[str] = []
    rejected: list[tuple] = []

    class InvalidBoundaryThenFixed:
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
            if output_type is B12Output:
                return B12Output.model_validate({
                    "pipeline_stage": "B1.2",
                    "block_id": 1,
                    "beats": [{
                        "beat_id": "1A",
                        "text": script,
                        "beat_type": "setup",
                    }],
                    "error": None,
                })
            assert output_type is ProfessionalB2Output
            attempts.append(instructions)
            if len(attempts) == 2:
                assert "FIRST beat of an intro" in instructions
                assert "LAST beat of a close" in instructions
            block = json.loads(input_text)["blocks"][0]
            if len(attempts) == 1:
                block["beats"][0].update(
                    visual_type="media_image",
                    prompt="A quiet still-life photograph",
                    video_prompt=None, reference=False, audio=False,
                )
            else:
                block["beats"][0].update(
                    visual_type="avatar",
                    prompt=None, video_prompt=None, reference=False, audio=True,
                )
            return ProfessionalB2Output.model_validate({
                "pipeline_stage": "B2",
                "narrative_core": {
                    "central_question": "What?", "final_answer": "Answer."
                },
                "blocks": [block], "error": None,
            })

    result = asyncio.run(run_b_pipeline(
        script, provider=InvalidBoundaryThenFixed(),
        visual_style="profesional", avatar_image=_avatar_png(),
        on_rejected_b2_output=lambda *args: rejected.append(args),
    ))
    assert len(attempts) == 2
    assert len(rejected) == 1
    beat = result.b2[0].blocks[0].beats[0]
    assert (beat.visual_type, beat.prompt, beat.video_prompt, beat.reference, beat.audio) == (
        "avatar", None, None, False, True
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
                video_prompt=(
                    "Speaking presenter makes a gentle gesture in sync with speech."
                    if len(attempts) == 2 else None
                ),
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
    assert "fp8none" in jobs[0].request.parameters["generation_profile"]
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


def test_speaking_media_video_adds_video_prompt_to_a2v_without_rewriting_avatar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = tmp_path / "avatar.png"
    avatar.write_bytes(_avatar_png())
    master = tmp_path / "master.wav"
    with wave.open(str(master), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(bytes(48000 * 2))
    monkeypatch.setattr(
        b_video, "_source_assets",
        lambda *_: (avatar, {(1, "1A"): avatar}),
    )
    monkeypatch.setattr(b_video, "_verified_asset", lambda *_: master)
    monkeypatch.setattr(
        b_video, "reference_num_frames_for_samples", lambda *_, **__: 25,
    )
    motion = (
        "She speaks naturally in sync with the supplied speech, "
        "blinks, and makes one small, relaxed hand gesture."
    )
    beat = {
        "block_id": 1, "beat_id": "1A", "visual_type": "media_video",
        "text": "Here is how I use it.", "description": "Face visible.",
        "prompt": "Face visible.", "video_prompt": motion,
        "audio": True, "start_seconds": 0.0, "end_seconds": 1.0,
    }
    timeline = {"beats": [beat], "duration_seconds": 1.0}
    monkeypatch.setattr(b_video, "build_stt_beat_timeline", lambda *_: timeline)
    plan = {"visual_style": "profesional"}
    audio = {"sha256": "b" * 64, "file": "master.wav"}
    _, jobs, _ = b_video.build_b2_video_plan(tmp_path, plan, audio, {})
    assert len(jobs) == 1
    assert jobs[0].kind == "a2v"
    assert jobs[0].request.parameters["prompt"] == (
        b_video.AVATAR_PROMPT + " " + motion
    )
    assert jobs[0].request.parameters["prompt"].startswith(b_video.AVATAR_PROMPT)


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
    monkeypatch.setattr(pipeline_cli, "load_dotenv", lambda *args, **kwargs: False)
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
