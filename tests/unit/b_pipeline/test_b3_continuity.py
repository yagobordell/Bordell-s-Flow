"""Regression coverage for professional B3 spoken-run camera continuity."""

import asyncio
import json

import pytest

from ai_video_factory.bots.contracts import (
    B3Output,
    B11Output,
    B12Output,
    ProfessionalB2Output,
)
from ai_video_factory.bots.workflow import (
    BPipelineResult,
    BPipelineValidationError,
    _B3GridLock,
    _professional_b3_groups,
    run_b_pipeline,
    validate_b3,
)
from ai_video_factory.workflows.b_video import _group_beats


def _professional_b2() -> ProfessionalB2Output:
    return ProfessionalB2Output.model_validate({
        "pipeline_stage": "B2",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{
            "block_id": 1,
            "type": "development",
            "emotional_entry": "A",
            "emotional_exit": "B",
            "span": {"first_words": "One", "last_words": "Three"},
            "beats": [
                {
                    "beat_id": "1A", "text": "One.", "beat_type": "claim",
                    "visual_type": "avatar", "prompt": None, "video_prompt": None,
                    "reference": False, "audio": True,
                },
                {
                    "beat_id": "1B", "text": "Two.", "beat_type": "claim",
                    "visual_type": "avatar_media",
                    "prompt": "A fixed support image beside the narrator.",
                    "video_prompt": None,
                    "reference": False, "audio": True,
                },
                {
                    "beat_id": "1C", "text": "Three.", "beat_type": "claim",
                    "visual_type": "avatar", "prompt": None, "video_prompt": None,
                    "reference": False, "audio": True,
                },
            ],
        }],
        "error": None,
    })


def _b3() -> B3Output:
    return B3Output.model_validate({
        "pipeline_stage": "B3",
        "group_id": 1,
        "beats": [
            {
                "beat_id": "1A", "continuous": False, "shot_prompt": None,
                "shot_video_prompt": None,
            },
            {
                "beat_id": "1B", "continuous": True, "shot_prompt": None,
                "shot_video_prompt": None,
            },
            {
                "beat_id": "1C", "continuous": False,
                "shot_prompt": (
                    "Same seated narrator in the same setup, now from a tighter "
                    "three-quarter camera angle."
                ),
                "shot_video_prompt": "Natural synced speech with a small hand gesture.",
            },
        ],
        "error": None,
    })


def test_b3_runs_only_for_three_plus_beats_with_non_avatar_visual() -> None:
    eligible = _professional_b2()

    all_avatar_payload = eligible.model_dump()
    for beat in all_avatar_payload["blocks"][0]["beats"]:
        beat.update(
            visual_type="avatar",
            prompt=None,
            video_prompt=None,
            reference=False,
            audio=True,
        )
    all_avatar = ProfessionalB2Output.model_validate(all_avatar_payload)

    short_payload = eligible.model_dump()
    short_payload["blocks"][0]["beats"] = short_payload["blocks"][0]["beats"][:2]
    short_payload["blocks"][0]["span"]["last_words"] = "Two"
    short = ProfessionalB2Output.model_validate(short_payload)

    assert _professional_b3_groups((eligible,)) != []
    assert _professional_b3_groups((all_avatar,)) == []
    assert _professional_b3_groups((short,)) == []


def test_b3_take_start_may_preserve_b2_without_video_prompt() -> None:
    result = B3Output.model_validate({
        "pipeline_stage": "B3",
        "group_id": 1,
        "beats": [
            {
                "beat_id": "1A",
                "continuous": False,
                "shot_prompt": None,
                "shot_video_prompt": None,
            },
            {
                "beat_id": "1B",
                "continuous": True,
                "shot_prompt": None,
                "shot_video_prompt": None,
            },
            {
                "beat_id": "1C",
                "continuous": True,
                "shot_prompt": None,
                "shot_video_prompt": None,
            },
        ],
        "error": None,
    })
    source = [
        (1, beat.model_dump()) for beat in _professional_b2().blocks[0].beats
    ]
    validate_b3(result, group_id=1, source=source, grid_locks={})


def test_b3_visual_plan_adds_take_boundaries_without_rewriting_b2() -> None:
    b2 = _professional_b2()
    b11 = B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{
            "block_id": 1, "type": "development",
            "emotional_entry": "A", "emotional_exit": "B",
            "span": {"first_words": "One", "last_words": "Three"},
        }],
        "error": None,
    })
    b12 = B12Output.model_validate({
        "pipeline_stage": "B1.2", "block_id": 1,
        "beats": [
            {"beat_id": "1A", "text": "One.", "beat_type": "claim"},
            {"beat_id": "1B", "text": "Two.", "beat_type": "claim"},
            {"beat_id": "1C", "text": "Three.", "beat_type": "claim"},
        ],
        "error": None,
    })
    result = BPipelineResult(
        b11=b11, b12=(b12,), b2=(b2,), b3=(_b3(),), visual_style="profesional"
    )
    beats = result.visual_plan()["blocks"][0]["beats"]
    assert [beat["continuous"] for beat in beats] == [False, True, False]
    assert beats[0]["b3_shot_prompt"] is None
    assert beats[1]["b3_shot_prompt"] is None
    assert beats[2]["b3_reference_beat_id"] == "1B"
    assert b2.blocks[0].beats[2].prompt is None


def test_b3_nonfirst_new_take_requires_continuity_prompt() -> None:
    source = [
        (1, beat.model_dump()) for beat in _professional_b2().blocks[0].beats
    ]
    invalid = B3Output.model_validate({
        "pipeline_stage": "B3",
        "group_id": 1,
        "beats": [
            {
                "beat_id": "1A", "continuous": False, "shot_prompt": None,
                "shot_video_prompt": "Natural speech.",
            },
            {
                "beat_id": "1B", "continuous": False, "shot_prompt": None,
                "shot_video_prompt": "Natural speech.",
            },
            {
                "beat_id": "1C", "continuous": True, "shot_prompt": None,
                "shot_video_prompt": None,
            },
        ],
        "error": None,
    })
    with pytest.raises(BPipelineValidationError, match="requires a continuity shot_prompt"):
        validate_b3(invalid, group_id=1, source=source, grid_locks={})


def test_b3_fixed_grid_speaker_is_mandatory_take_start() -> None:
    source = [
        (1, beat.model_dump()) for beat in _professional_b2().blocks[0].beats
    ]
    locks = {
        (1, "1B"): _B3GridLock(
            grid_id="1:1B", source="numbered", role="speaker_start"
        )
    }
    invalid = B3Output.model_validate({
        "pipeline_stage": "B3",
        "group_id": 1,
        "beats": [
            {
                "beat_id": "1A", "continuous": False, "shot_prompt": None,
                "shot_video_prompt": "Natural speech.",
            },
            {
                "beat_id": "1B", "continuous": True, "shot_prompt": None,
                "shot_video_prompt": None,
            },
            {
                "beat_id": "1C", "continuous": True, "shot_prompt": None,
                "shot_video_prompt": None,
            },
        ],
        "error": None,
    })
    with pytest.raises(BPipelineValidationError, match="fixed panel"):
        validate_b3(invalid, group_id=1, source=source, grid_locks=locks)


def test_ltx_groups_professional_audio_by_b3_continuous_boundaries() -> None:
    beats = [
        {"block_id": 1, "beat_id": "1A", "visual_type": "avatar",
         "audio": True, "continuous": False},
        {"block_id": 1, "beat_id": "1B", "visual_type": "media_video",
         "audio": True, "continuous": True},
        {"block_id": 1, "beat_id": "1C", "visual_type": "avatar",
         "audio": True, "continuous": False},
        {"block_id": 1, "beat_id": "1D", "visual_type": "avatar_media",
         "audio": True, "continuous": True},
        {"block_id": 1, "beat_id": "1E", "visual_type": "media_video",
         "audio": False},
    ]
    assert _group_beats(beats) == [
        ("a2v", 0, 1),
        ("a2v", 2, 3),
        ("i2v", 4, 4),
    ]


def test_b3_accepts_multiple_fixed_grid_starts_as_continuity_exception() -> None:
    source = [
        (1, beat.model_dump()) for beat in _professional_b2().blocks[0].beats
    ]
    locks = {
        (1, "1A"): _B3GridLock(
            grid_id="grid-a", source="numbered", role="speaker_start"
        ),
        (1, "1C"): _B3GridLock(
            grid_id="grid-b", source="numbered", role="speaker_start"
        ),
    }
    result = B3Output.model_validate({
        "pipeline_stage": "B3",
        "group_id": 1,
        "beats": [
            {
                "beat_id": "1A", "continuous": False, "shot_prompt": None,
                "shot_video_prompt": "Speak naturally from fixed grid A.",
            },
            {
                "beat_id": "1B", "continuous": True, "shot_prompt": None,
                "shot_video_prompt": None,
            },
            {
                "beat_id": "1C", "continuous": False, "shot_prompt": None,
                "shot_video_prompt": "Speak naturally from fixed grid B.",
            },
        ],
        "error": None,
    })
    validate_b3(result, group_id=1, source=source, grid_locks=locks)


def test_run_b_pipeline_b3_groups_audio_across_block_boundaries() -> None:
    script = "Hello from here. Middle section now. Goodbye for now."
    b11 = B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [
            {
                "block_id": 1, "type": "intro",
                "emotional_entry": "A", "emotional_exit": "B",
                "span": {
                    "first_words": "Hello from here.",
                    "last_words": "Middle section now.",
                },
            },
            {
                "block_id": 2, "type": "close",
                "emotional_entry": "B", "emotional_exit": "C",
                "span": {
                    "first_words": "Goodbye for now.",
                    "last_words": "Goodbye for now.",
                },
            },
        ],
        "error": None,
    })
    b12 = (
        B12Output.model_validate({
            "pipeline_stage": "B1.2", "block_id": 1,
            "beats": [
                {
                    "beat_id": "1A",
                    "text": "Hello from here. ",
                    "beat_type": "claim",
                },
                {
                    "beat_id": "1B",
                    "text": "Middle section now. ",
                    "beat_type": "claim",
                },
            ],
            "error": None,
        }),
        B12Output.model_validate({
            "pipeline_stage": "B1.2", "block_id": 2,
            "beats": [{
                "beat_id": "2A",
                "text": "Goodbye for now.",
                "beat_type": "return",
            }],
            "error": None,
        }),
    )
    b3_inputs: list[dict] = []

    class Provider:
        async def generate_structured(
            self, *, model, instructions, input_text, output_type,
            input_image_url=None,
        ):
            payload = json.loads(input_text)
            if output_type is ProfessionalB2Output:
                block = payload["blocks"][0]
                for index, beat in enumerate(block["beats"]):
                    if block["block_id"] == 1 and index == 1:
                        beat.update(
                            visual_type="avatar_media",
                            prompt="A fixed support image beside the narrator.",
                            video_prompt=None,
                            reference=False,
                            audio=True,
                        )
                    else:
                        beat.update(
                            visual_type="avatar",
                            prompt=None,
                            video_prompt=None,
                            reference=False,
                            audio=True,
                        )
                return ProfessionalB2Output.model_validate({
                    "pipeline_stage": "B2",
                    "narrative_core": payload["narrative_core"],
                    "blocks": [block],
                    "error": None,
                })
            if output_type is not B3Output:
                raise RuntimeError(f"Unexpected output type: {output_type}")
            b3_inputs.append(payload)
            return B3Output.model_validate({
                "pipeline_stage": "B3",
                "group_id": payload["group_id"],
                "beats": [
                    {
                        "beat_id": payload["beats"][0]["beat_id"],
                        "continuous": False,
                        "shot_prompt": None,
                        "shot_video_prompt": None,
                    },
                    {
                        "beat_id": payload["beats"][1]["beat_id"],
                        "continuous": True,
                        "shot_prompt": None,
                        "shot_video_prompt": None,
                    },
                    {
                        "beat_id": payload["beats"][2]["beat_id"],
                        "continuous": True,
                        "shot_prompt": None,
                        "shot_video_prompt": None,
                    },
                ],
                "error": None,
            })

    result = asyncio.run(run_b_pipeline(
        script,
        provider=Provider(),
        start_from="B2",
        stop_after="B3",
        previous_b11=b11,
        previous_b12=b12,
        visual_style="profesional",
        avatar_image=b"avatar",
    ))
    assert len(b3_inputs) == 1
    assert [beat["beat_id"] for beat in b3_inputs[0]["beats"]] == [
        "1A", "1B", "2A"
    ]
    plan = result.visual_plan()
    assert plan["blocks"][0]["beats"][0]["continuous"] is False
    assert plan["blocks"][0]["beats"][0]["b3_shot_video_prompt"] is None
    assert plan["blocks"][0]["beats"][1]["continuous"] is True
    assert plan["blocks"][1]["beats"][0]["continuous"] is True


@pytest.mark.parametrize(
    ("texts", "mixed"),
    [
        (["First sentence here.", "Second sentence here."], True),
        (
            [
                "First sentence here.",
                "Second sentence here.",
                "Third sentence here.",
            ],
            False,
        ),
    ],
)
def test_run_b_pipeline_skips_ineligible_b3_groups(
    texts: list[str],
    mixed: bool,
) -> None:
    script = " ".join(texts)
    b11 = B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{
            "block_id": 1,
            "type": "development",
            "emotional_entry": "A",
            "emotional_exit": "B",
            "span": {
                "first_words": texts[0],
                "last_words": texts[-1],
            },
        }],
        "error": None,
    })
    b12 = (
        B12Output.model_validate({
            "pipeline_stage": "B1.2",
            "block_id": 1,
            "beats": [
                {
                    "beat_id": f"1{chr(ord('A') + index)}",
                    "text": text + (" " if index < len(texts) - 1 else ""),
                    "beat_type": "claim",
                }
                for index, text in enumerate(texts)
            ],
            "error": None,
        }),
    )

    class Provider:
        async def generate_structured(
            self, *, model, instructions, input_text, output_type,
            input_image_url=None,
        ):
            payload = json.loads(input_text)
            if output_type is B3Output:
                pytest.fail("B3 must not be called for an ineligible spoken run")
            if output_type is not ProfessionalB2Output:
                raise RuntimeError(f"Unexpected output type: {output_type}")
            block = payload["blocks"][0]
            for index, beat in enumerate(block["beats"]):
                if mixed and index == 1:
                    beat.update(
                        visual_type="avatar_media",
                        prompt="A fixed support image beside the narrator.",
                        video_prompt=None,
                        reference=False,
                        audio=True,
                    )
                else:
                    beat.update(
                        visual_type="avatar",
                        prompt=None,
                        video_prompt=None,
                        reference=False,
                        audio=True,
                    )
            return ProfessionalB2Output.model_validate({
                "pipeline_stage": "B2",
                "narrative_core": payload["narrative_core"],
                "blocks": [block],
                "error": None,
            })

    result = asyncio.run(run_b_pipeline(
        script,
        provider=Provider(),
        start_from="B2",
        stop_after="B3",
        previous_b11=b11,
        previous_b12=b12,
        visual_style="profesional",
        avatar_image=b"avatar",
    ))

    assert result.b3 == ()
    for beat in result.visual_plan()["blocks"][0]["beats"]:
        assert "continuous" not in beat
        assert "b3_shot_prompt" not in beat
        assert "b3_shot_video_prompt" not in beat
