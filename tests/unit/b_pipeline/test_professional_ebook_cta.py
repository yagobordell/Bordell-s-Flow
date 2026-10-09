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
        audio=audio, reference=True,
        reference_sources=["presenter", "ebook"] if audio else ["ebook"],
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
    assert _visual("1C", audio=True).reference_sources == ["presenter", "ebook"]
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
