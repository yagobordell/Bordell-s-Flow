"""Proof of actual ebook preflight, cross-beat grid and principled prompts."""
from __future__ import annotations

import pytest

from ai_video_factory.agents.professional.compat import project_professional_plan
from ai_video_factory.agents.professional.preflight import (
    requires_ebook_cover,
    validate_source_assets,
)
from ai_video_factory.agents.professional.prompt_loader import load_phase_instructions
from ai_video_factory.agents.professional.validation import validate_plan


def test_real_spoken_ebook_promotion_requires_cover_before_any_model_use():
    source = (
        "There is practical advice in this lesson. "
        "Download my guide as an ebook to keep it close. "
        "Scan the QR code on your screen to get your ebook."
    )
    assert requires_ebook_cover(source)
    with pytest.raises(ValueError, match="PREFLIGHT_EBOOK_COVER_REQUIRED"):
        validate_source_assets(source, ebook_available=False)
    validate_source_assets(source, ebook_available=True)


def test_non_ebook_video_remains_usable_without_a_cover():
    source = (
        "An incoming call seems to be from the bank. "
        "Hang up and use a number you looked up independently."
    )
    assert not requires_ebook_cover(source)
    validate_source_assets(source, ebook_available=False)


def test_merely_discussing_ebooks_is_not_a_protected_offer():
    source = (
        "Some people read ebooks on their tablets. "
        "Their phone receives an ordinary bank call."
    )
    assert not requires_ebook_cover(source)
    validate_source_assets(source, ebook_available=False)


def _source_and_plan(*, first_grid: bool = True):
    source = "She reaches for the ringing phone. She checks the physical card. This is the lesson."
    groups = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "curiosity", "emotional_exit": "clarity",
        "span": {
            "first_words": "She reaches for the ringing phone.",
            "last_words": "This is the lesson.",
        },
    }]
    fragments = [
        "She reaches for the ringing phone. ",
        "She checks the physical card. ",
        "This is the lesson.",
    ]
    beats = []
    for index, text in enumerate(fragments):
        media = index < 2
        beats.append({
            "beat_id": f"1{chr(65 + index)}", "beat_type": (
                "action" if media else "verdict"
            ),
            "text": text,
            "visual_type": "media_image" if media else "avatar",
            "prompt": None, "video_prompt": None, "reference": False,
            "audio": not media,
            **({
                "image_prompts": [
                    "A woman in the same room with her ringing phone."
                    if index == 0 else
                    "The same woman's hand compares her physical card."
                ],
                "generation_mode": (
                    "grid" if index == 1 or first_grid else "independent"
                ),
                "editor_notes": [],
            } if media else {"editor_notes": []}),
        })
    plan = {
        "visual_style": "profesional",
        "narrative_core": {
            "central_question": "Who is calling?",
            "final_answer": "Verify the identity independently.",
        },
        "blocks": [{"block_id": 1, "beats": beats}],
    }
    return source, groups, plan


def test_two_single_still_beats_form_one_continuous_numbered_grid():
    source, groups, plan = _source_and_plan()
    report = validate_plan(source, groups, plan)
    assert report["status"] == "passed", report["issues"]
    projection = project_professional_plan(source, groups, plan)
    visual_beats = projection["visual_plan"]["blocks"][0]["beats"]
    assert visual_beats[0]["prompt"].startswith("1. ")
    assert visual_beats[1]["prompt"].startswith("2. ")
    from ai_video_factory.providers.image_grids import plan_image_jobs
    from ai_video_factory.providers.openai_batch_images import described_beats

    jobs = plan_image_jobs(
        projection["visual_plan"], described_beats(projection["visual_plan"]),
    )
    grids = [job for job in jobs if job.get("kind") == "grid"]
    assert len(grids) == 1
    assert grids[0]["grid"]["numbered_beat_ids"] == ["1A", "1B"]
    assert grids[0]["grid"]["numbered_asset_ids"] == ["1A", "1B"]


def test_orphan_single_grid_asset_is_invalid_but_same_image_can_be_independent():
    source, groups, plan = _source_and_plan(first_grid=False)
    report = validate_plan(source, groups, plan)
    assert report["status"] == "blocked"
    assert "GRID_SEQUENCE_SIZE" in {issue["rule_id"] for issue in report["issues"]}
    plan["blocks"][0]["beats"][1]["generation_mode"] = "independent"
    assert validate_plan(source, groups, plan)["status"] == "passed"


def test_positive_direction_rules_and_carriers_are_visible_to_model():
    content = load_phase_instructions("02_planning")
    assert "rules/direction_continuity.md" in content
    assert "track the participant\u0027s" in content
    assert "grid" in content
    assert "image_prompts" in content
    assert "prompt=null" in content
    assert "postproduction" in content
    assert "source-anchored" in content
    assert "Atomic-event test" in content
    assert "Rhythmic-cohesion test" in content
    assert "Correct segmentation:" not in content
    assert "Source:" not in content
    assert "For example," not in content
    assert "ONE global beat + visual direction" in content


def test_real_spoken_ebook_and_qr_beats_cannot_be_silently_unprotected():
    script = "My ebook helps you remember. Scan the QR code to get it. Stay safe."
    groups = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "curiosity", "emotional_exit": "confidence",
        "span": {
            "first_words": "My ebook helps you remember.",
            "last_words": "Stay safe.",
        },
    }]
    texts = [
        "My ebook helps you remember. ",
        "Scan the QR code to get it. ",
        "Stay safe.",
    ]
    beats = []
    for index, text in enumerate(texts):
        media = index < 2
        beats.append({
            "beat_id": f"1{chr(65 + index)}",
            "beat_type": "claim" if media else "verdict",
            "text": text,
            "visual_type": "media_image" if media else "avatar",
            "prompt": None, "video_prompt": None,
            "reference": False, "audio": not media,
            "editor_notes": [],
            **({
                "image_prompts": ["Authentic ebook-related still in a home."],
                "generation_mode": "independent",
            } if media else {}),
        })
    plan = {
        "visual_style": "profesional",
        "narrative_core": {
            "central_question": "How to stay safe?",
            "final_answer": "Remember the warning signs.",
        },
        "blocks": [{"block_id": 1, "beats": beats}],
    }
    report = validate_plan(script, groups, plan, ebook_available=True)
    assert report["status"] == "blocked"
    codes = {issue["rule_id"] for issue in report["issues"]}
    assert "CTA_SPOKEN_ASSET_UNPROTECTED" in codes
    assert "CTA_SPOKEN_OFFER_MISSING" in codes
    for beat in beats[:2]:
        beat["cta_group_id"] = 1
        beat["cta_reference_sources"] = ["ebook"]
        beat["reference"] = True
    result = validate_plan(script, groups, plan, ebook_available=True)
    assert result["status"] == "passed", result["issues"]
