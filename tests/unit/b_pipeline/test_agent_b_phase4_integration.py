"""Phase 4: adapter preserves source and CTA ownership without creative bot calls."""

import copy

import pytest

from ai_video_factory.bots.agent_b.integration import (
    AgentBIntegrationError,
    assert_cta_detector_match,
    declared_cta_groups,
    legacy_consumer_views,
)
from ai_video_factory.bots.contracts import B11Output


def _b11():
    return B11Output.model_validate({
        "pipeline_stage": "B1.1", "narrative_core": {
            "central_question": "What?", "final_answer": "A warning.",
        },
        "blocks": [{
            "block_id": 1, "type": "intro", "emotional_entry": "suspense",
            "emotional_exit": "uncertainty", "span": {
                "first_words": "A phone rings.", "last_words": "A phone rings.",
            },
        }],
        "error": None,
    })


def _plan():
    b11 = _b11()
    return {
        "schema_version": "b-pipeline-v1", "visual_style": "profesional",
        "narrative_core": b11.narrative_core.model_dump(),
        "blocks": [{
            "block_id": 1, "type": "intro", "emotional_entry": "suspense",
            "emotional_exit": "uncertainty", "beats": [{
                "beat_id": "1A", "text": "A phone rings.", "beat_type": "action",
                "visual_type": "media_image", "prompt": "A phone on a table.",
                "video_prompt": None, "reference": False, "audio": False,
            }],
        }],
    }


def test_downstream_views_preserve_exact_metadata_without_new_creative_outputs():
    plan = _plan()
    b12, b2 = legacy_consumer_views(plan, _b11())
    assert len(b12) == len(b2) == 1
    assert b12[0].beats[0].text == "A phone rings."
    assert b2[0].blocks[0].span.first_words == "A phone rings."
    assert b2[0].blocks[0].beats[0].prompt == "A phone on a table."
    assert "visual_subshots" not in b2[0].blocks[0].beats[0].model_dump()
    assert assert_cta_detector_match(plan, []) == ()


def test_matching_group_does_not_add_new_ebook_or_avatar_decisions():
    plan = _plan()
    beat = plan["blocks"][0]["beats"][0]
    beat.update({"cta_group_id": 1, "cta_reference_sources": ["ebook"],
                 "reference": True})
    assert declared_cta_groups(plan) == (("1A",),)
    assert assert_cta_detector_match(plan, [["1A"]]) == (("1A",),)
    with pytest.raises(AgentBIntegrationError, match="disagree"):
        assert_cta_detector_match(plan, [])


def test_detected_cta_cannot_be_silently_added_to_frozen_plan():
    plan = _plan()
    with pytest.raises(AgentBIntegrationError, match="disagree"):
        assert_cta_detector_match(plan, [["1A"]])


def test_cta_requires_explicit_per_beat_reference_sources():
    plan = _plan()
    plan["blocks"][0]["beats"][0]["cta_group_id"] = 1
    with pytest.raises(AgentBIntegrationError, match="explicit"):
        declared_cta_groups(plan)


def test_b11_identity_does_not_change_through_adapter():
    plan = _plan()
    corrupted = copy.deepcopy(plan)
    corrupted["blocks"][0]["emotional_entry"] = "different"
    with pytest.raises(AgentBIntegrationError, match="B1.1 block"):
        legacy_consumer_views(corrupted, _b11())
