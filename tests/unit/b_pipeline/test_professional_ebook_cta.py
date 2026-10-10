"""Contracts and deterministic safeguards for professional ebook CTA pipeline."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_video_factory.bots.professional_cta import (
    ProfessionalCTADetection,
    ProfessionalCTAVisualBeat,
)
from ai_video_factory.bots.professional_cta_workflow import (
    apply_cta_visuals,
    detect_professional_ctas,
    direct_professional_cta,
    validate_cta_detection,
)
from ai_video_factory.compositor.professional_cta_qr import (
    professional_cta_windows,
)
from ai_video_factory.workflows.b_video import _group_beats


def _merged():
    return {
        "narrative_core": {"central_question": "Why?", "final_answer": "Verify."},
        "blocks": [
            {
                "block_id": 1, "type": "development",
                "beats": [
                    {"beat_id": "1A", "beat_type": "claim", "text": "The opening."},
                    {"beat_id": "1B", "beat_type": "claim", "text": "Try my ebook."},
                    {"beat_id": "1C", "beat_type": "claim", "text": "Scan the QR."},
                ],
            },
            {
                "block_id": 2, "type": "close",
                "beats": [
                    {"beat_id": "2A", "beat_type": "claim", "text": "Another chapter."},
                    {"beat_id": "2B", "beat_type": "claim", "text": "Scan for my ebook."},
                    {"beat_id": "2C", "beat_type": "claim", "text": "Goodbye."},
                ],
            },
        ],
    }


def _visual(beat_id, *, audio=False):
    return ProfessionalCTAVisualBeat(
        beat_id=beat_id, visual_type="media_video",
        prompt="A precise close view of the actual reference ebook on a desk.",
        video_prompt="The camera glides slowly closer to the book.",
        audio=False, reference=True,
        reference_sources=["ebook"],
    )


def test_detector_accepts_cross_block_complete_beats_and_rejects_invalid_ids():
    source = _merged()
    assert validate_cta_detection(
        ProfessionalCTADetection(cta_groups=[["1B", "1C"], ["2B"]]),
        source,
    ) == (("1B", "1C"), ("2B",))
    for groups in (
        [["1C", "1B"]], [["1A", "2B"]], [["1B", "1B"]],
        [["1A"], ["1A"]], [["9Z"]],
    ):
        with pytest.raises(ValueError):
            validate_cta_detection(ProfessionalCTADetection(cta_groups=groups), source)


def test_cta_beat_contract_requires_correct_references_and_audio():
    assert _visual("1B").reference_sources == ["ebook"]
    assert _visual("1C", audio=True).reference_sources == ["ebook"]
    with pytest.raises(ValidationError):
        ProfessionalCTAVisualBeat(
            beat_id="1A", visual_type="media_video",
            prompt="The book.", video_prompt="Move.", audio=True,
            reference=True, reference_sources=["ebook"],
        )


def test_merger_replaces_only_detected_beats_and_freezes_metadata():
    blocks = [
        {**block, "beats": [
            {**beat, "visual_type": "media_image",
             "prompt": "Ordinary B2 decision.", "video_prompt": None,
             "reference": False, "audio": False}
            for beat in block["beats"]
        ]} for block in _merged()["blocks"]
    ]
    new = apply_cta_visuals(
        blocks, (("1B", "1C"), ("2B",)),
        {"1B": _visual("1B"), "1C": _visual("1C"), "2B": _visual("2B")},
    )
    assert new[0]["beats"][0]["prompt"] == "Ordinary B2 decision."
    assert new[0]["beats"][1]["cta_reference_sources"] == ["ebook"]
    assert new[0]["beats"][1]["reference"] is True
    assert new[0]["beats"][1]["text"] == "Try my ebook."
    assert new[0]["beats"][2]["cta_group_id"] == 1
    assert new[1]["beats"][1]["cta_group_id"] == 2
    assert new[1]["beats"][2]["prompt"] == "Ordinary B2 decision."
    assert "cta_group_id" not in blocks[0]["beats"][1]


def test_b3_audio_grouping_and_ltx_cta_boundary_are_isolated():
    beats = [
        {"beat_id": "1A", "block_id": 1, "visual_type": "avatar", "audio": True},
        {"beat_id": "1B", "block_id": 1, "visual_type": "avatar", "audio": True,
         "cta_group_id": 1},
        {"beat_id": "1C", "block_id": 1, "visual_type": "avatar", "audio": True,
         "cta_group_id": 1},
        {"beat_id": "1D", "block_id": 1, "visual_type": "avatar", "audio": True},
    ]
    assert _group_beats(beats) == [
        ("a2v", 0, 0), ("a2v", 1, 2), ("a2v", 3, 3),
    ]


def test_real_qr_windows_use_cta_narration_and_one_minute_half_tail():
    timeline = {
        "duration_seconds": 220.0,
        "beats": [
            {"beat_id": "1A", "start_seconds": 0.0, "end_seconds": 10.0},
            {"beat_id": "1B", "start_seconds": 10.0, "end_seconds": 25.0},
            {"beat_id": "1C", "start_seconds": 25.0, "end_seconds": 35.0},
            {"beat_id": "2A", "start_seconds": 175.0, "end_seconds": 185.0},
        ],
    }
    assert professional_cta_windows(
        timeline, {"ebook_cta_groups": [["1B", "1C"], ["2A"]]},
    ) == [(10.0, 125.0), (175.0, 185.0)]


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.inputs = []

    async def generate_structured_with_response(self, **kwargs):
        self.inputs.append(kwargs)
        return self.responses.pop(0), SimpleNamespace(usage=None)


def test_detector_and_director_both_use_model_with_exact_ids_and_two_images():
    from ai_video_factory.bots.professional_cta import ProfessionalCTAVisuals

    provider = FakeProvider([
        ProfessionalCTADetection(cta_groups=[["1B", "1C"]]),
        ProfessionalCTAVisuals(beats=[_visual("1B"), _visual("1C")]),
    ])
    async def run():
        sem = asyncio.Semaphore(3)
        found = await detect_professional_ctas(
            _merged(), provider=provider, model="test", semaphore=sem,
        )
        selected = await direct_professional_cta(
            1, found[0], _merged(),
            presenter=b"presenter", ebook=b"ebook",
            provider=provider, model="test", semaphore=sem,
        )
        return found, selected
    found, selected = asyncio.run(run())
    assert found == (("1B", "1C"),)
    assert set(selected) == {"1B", "1C"}
    assert len(provider.inputs[1]["input_image_urls"]) == 2
    assert provider.inputs[1]["output_type"] is ProfessionalCTAVisuals


def test_b21_never_changes_cta_prompts_even_when_patch_is_provided():
    from ai_video_factory.bots.contracts import B21Output
    from ai_video_factory.bots.workflow import apply_b21_review

    original = {
        "visual_style": "profesional",
        "blocks": [{"block_id": 1, "beats": [{
            "beat_id": "1B", "visual_type": "media_video",
            "prompt": "CTA director image.", "video_prompt": "CTA movement.",
            "cta_group_id": 1,
        }]}],
    }
    patch = B21Output.model_validate({
        "pipeline_stage": "B2.1",
        "error": None,
        "modified_beats": [{
            "block_id": 1, "beat_id": "1B",
            "prompt": "B2.1 replacement.", "video_prompt": "Wrong motion.",
            "second_prompt": None, "split_mode": None,
        }],
    })
    output = apply_b21_review(original, patch)
    assert output["blocks"][0]["beats"][0]["prompt"] == "CTA director image."
    assert output["blocks"][0]["beats"][0]["video_prompt"] == "CTA movement."


def test_b3_never_combines_speaking_cta_with_normal_run():
    from ai_video_factory.bots.workflow import _professional_audio_groups

    plan = {"visual_style": "profesional", "blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "audio": True},
        {"beat_id": "1B", "audio": True, "cta_group_id": 1},
        {"beat_id": "1C", "audio": True, "cta_group_id": 1},
        {"beat_id": "1D", "audio": True},
    ]}]}
    assert [
        [beat["beat_id"] for _, beat in group]
        for group in _professional_audio_groups(plan)
    ] == [["1A"], ["1B", "1C"], ["1D"]]


def test_full_b2_cta_merge_discard_b21_patch_and_preserve_normal_decisions():
    import json

    from ai_video_factory.bots.contracts import (
        B11Output,
        B12Output,
        B21Output,
        ProfessionalB2Output,
    )
    from ai_video_factory.bots.professional_cta import ProfessionalCTAVisuals
    from ai_video_factory.bots.workflow import run_b_pipeline

    script = "First scene. Buy my ebook. Last scene."
    b11 = B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{
            "block_id": 1, "type": "development",
            "emotional_entry": "A", "emotional_exit": "B",
            "span": {"first_words": "First scene.", "last_words": "Last scene."},
        }], "error": None,
    })
    b12 = (B12Output.model_validate({
        "pipeline_stage": "B1.2", "block_id": 1, "error": None,
        "beats": [
            {"beat_id": "1A", "text": "First scene. ", "beat_type": "claim"},
            {"beat_id": "1B", "text": "Buy my ebook. ", "beat_type": "claim"},
            {"beat_id": "1C", "text": "Last scene.", "beat_type": "claim"},
        ],
    }),)
    assert "".join(beat.text for beat in b12[0].beats) == script
    captured_b21 = []

    class Provider:
        async def generate_structured(
            self, *, model, instructions, input_text, output_type,
            input_image_url=None,
        ):
            payload = json.loads(input_text)
            if output_type is ProfessionalB2Output:
                block = payload["blocks"][0]
                for beat in block["beats"]:
                    beat.update({
                        "visual_type": "media_image", "audio": False,
                        "reference": False, "video_prompt": None,
                        "prompt": "Ordinary B2 static composition.",
                    })
                return ProfessionalB2Output.model_validate({
                    "pipeline_stage": "B2",
                    "narrative_core": payload["narrative_core"],
                    "blocks": [block], "error": None,
                })
            if output_type is B21Output:
                captured_b21.append(payload)
                return B21Output.model_validate({
                    "pipeline_stage": "B2.1", "error": None,
                    "modified_beats": [{
                        "block_id": 1, "beat_id": "1B",
                        "prompt": "MUST BE DISCARDED",
                        "video_prompt": "MUST BE DISCARDED",
                        "second_prompt": None, "split_mode": None,
                    }],
                })
            raise RuntimeError("Unexpected model call")

        async def generate_structured_with_response(self, **kwargs):
            typ = kwargs["output_type"]
            if typ in {ProfessionalB2Output, B21Output}:
                result = await self.generate_structured(**kwargs)
                return result, SimpleNamespace(usage=None)
            if typ is ProfessionalCTADetection:
                return (
                    ProfessionalCTADetection(cta_groups=[["1B"]]),
                    SimpleNamespace(usage=None),
                )
            if typ is ProfessionalCTAVisuals:
                return ProfessionalCTAVisuals(beats=[_visual("1B")]), SimpleNamespace(
                    usage=None,
                )
            raise RuntimeError("Unexpected CTA output type")

    result = asyncio.run(run_b_pipeline(
        script, provider=Provider(), start_from="B2", stop_after="B2.1",
        visual_style="profesional",
        avatar_image=b"presenter", ebook_image=b"ebook",
        previous_b11=b11, previous_b12=b12,
    ))
    assert result.cta_groups == (("1B",),)
    normal = result.b2[0].blocks[0].beats[1]
    assert normal.prompt == "Ordinary B2 static composition."
    final = result.visual_plan()["blocks"][0]["beats"]
    assert final[0]["prompt"] == "Ordinary B2 static composition."
    assert final[1]["prompt"] == _visual("1B").prompt
    assert final[1]["visual_type"] == "media_video"
    assert final[1]["reference"] is True
    assert final[1]["cta_reference_sources"] == ["ebook"]
    assert final[2]["prompt"] == "Ordinary B2 static composition."
    assert captured_b21
    assert "cta_reference_sources" not in captured_b21[0]["blocks"][0]["beats"][1]
