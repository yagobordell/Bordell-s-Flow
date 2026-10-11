"""Whole-script Professional agent hard-rule regression tests (no network)."""
from __future__ import annotations

import copy

import pytest

from ai_video_factory.agents.professional.tools import ProfessionalPlanWorkspace
from ai_video_factory.agents.professional.validation import validate_groups, validate_plan


def sample():
    parts = ["Una idea concreta. ", "Otra idea clara. ", "Una tercera prueba. ", "Y cerramos."]
    script = "".join(parts)
    groups = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "neutro", "emotional_exit": "conclusion",
        "span": {"first_words": "Una idea concreta.", "last_words": "Y cerramos."},
    }]
    beats = []
    for idx, text in enumerate(parts, 1):
        intro = {
            "beat_id": f"1{chr(64 + idx)}",
            "text": text,
            "beat_type": "claim",
            "visual_type": "media_image",
            "prompt": None, "video_prompt": None,
            "audio": False, "reference": False,
            "image_prompts": [f"Meaningful image {idx}"],
            "generation_mode": "independent",
            "editor_notes": [],
        }
        beats.append(intro)
    beats[-1] = {
        "beat_id": "1D", "text": parts[-1], "beat_type": "return",
        "visual_type": "avatar", "prompt": None,
        "video_prompt": None, "audio": True, "reference": False,
        "editor_notes": [],
    }
    plan = {
        "visual_style": "profesional",
        "narrative_core": {"central_question": "¿Qué?", "final_answer": "Una conclusión."},
        "blocks": [{"block_id": 1, "beats": beats}],
    }
    return script, groups, plan


def codes(report):
    return {issue["rule_id"] for issue in report["issues"]}


def test_b11_anchors_materialize_full_script_exactly():
    script, groups, _ = sample()
    result = validate_groups(script, groups)
    assert result["status"] == "passed"
    assert result["groups"][0]["text"] == script


def test_plan_valid_and_avatar_under_45_percent():
    script, groups, plan = sample()
    result = validate_plan(script, groups, plan)
    assert result["status"] == "passed"
    assert result["summary"]["avatar_beats"] == 1
    assert result["summary"]["avatar_percentage"] == 25.0
    assert result["rhythm_map"]["basis"] == "script_characters_not_seconds"


def test_punctuation_change_fails_exact_source():
    script, groups, plan = sample()
    plan["blocks"][0]["beats"][0]["text"] = "Una idea concreta! "
    assert "SOURCE_INTEGRITY" in codes(validate_plan(script, groups, plan))


def test_70_character_image_beat_requires_two_prompts_and_max_four():
    text = "X" * 70
    suffixes = [" Otro. ", " Más. ", " Fin."]
    script, groups, plan = sample()
    plan["blocks"][0]["beats"][0]["text"] = text
    for beat, content in zip(plan["blocks"][0]["beats"][1:], suffixes, strict=True):
        beat["text"] = content
    script = text + "".join(suffixes)
    groups[0]["span"] = {"first_words": text, "last_words": "Fin."}

    result = validate_plan(script, groups, plan)
    assert "LONG_IMAGE_SPLIT" in codes(result)
    plan["blocks"][0]["beats"][0]["image_prompts"] = ["View A", "View B", "View C", "View D"]
    result = validate_plan(script, groups, plan)
    assert result["status"] == "passed"
    assert result["rhythm_map"]["within_beat_changes"] == 3
    plan["blocks"][0]["beats"][0]["image_prompts"].append("View E")
    assert "IMAGE_PROMPT_COUNT" in codes(validate_plan(script, groups, plan))


def test_avatar_exactly_45_percent_fails():
    script, groups, plan = sample()
    beats = []
    parts = []
    for i in range(20):
        token = f"Número {i}. "
        parts.append(token)
        item = copy.deepcopy(plan["blocks"][0]["beats"][0])
        item["beat_id"] = f"1{chr(65 + i)}"
        item["text"] = token
        if i % 2 == 0 and i < 18:
            item["visual_type"] = "avatar"
            item["image_prompts"] = []
            item["audio"] = True
        beats.append(item)
    # Last beat remains original avatar; replace one earlier avatar to keep 9 total.
    beats[0]["visual_type"] = "media_image"
    beats[0]["image_prompts"] = ["Another image"]
    beats[0]["audio"] = False
    beats[-1]["visual_type"] = "avatar"
    beats[-1]["image_prompts"] = []
    beats[-1]["audio"] = True
    plan["blocks"][0]["beats"] = beats
    script = "".join(parts)
    groups[0]["span"] = {
        "first_words": parts[0].strip(),
        "last_words": parts[-1].strip(),
    }
    result = validate_plan(script, groups, plan)
    assert result["summary"]["avatar_beats"] == 9
    assert result["summary"]["avatar_percentage"] == 45.0
    assert "AVATAR_RATIO" in codes(result)


def test_spoken_run_includes_avatar_media():
    script, groups, plan = sample()
    beats = plan["blocks"][0]["beats"]
    beats[0]["visual_type"] = "avatar_media"
    beats[0]["prompt"] = "A supporting paper."
    beats[0]["image_prompts"] = []
    beats[0]["audio"] = True
    beats[1]["visual_type"] = "avatar_media"
    beats[1]["prompt"] = "A supporting table."
    beats[1]["image_prompts"] = []
    beats[1]["audio"] = True
    # Inflate both narrations to 70 chars without exceeding per-beat cap.
    beats[0]["text"] = "A" * 70 + " "
    beats[1]["text"] = "B" * 70 + " "
    script = "".join(b["text"] for b in beats)
    groups[0]["span"] = {
        "first_words": "A" * 70,
        "last_words": "Y cerramos.",
    }
    assert "SPEAKING_RUN" in codes(validate_plan(script, groups, plan))


def test_editor_trigger_occurrence_and_cta_freeze():
    script, groups, plan = sample()
    beats = plan["blocks"][0]["beats"]
    beats[0]["editor_notes"] = [{
        "kind": "numeric_reveal",
        "trigger": {"text": "concreta", "occurrence": 2},
    }]
    assert "EDITOR_TRIGGER" in codes(validate_plan(script, groups, plan))
    beats[0]["editor_notes"][0]["trigger"]["occurrence"] = 1
    assert "EDITOR_TRIGGER" not in codes(validate_plan(script, groups, plan))

    before = copy.deepcopy(beats[0])
    beats[0]["image_prompts"] = ["A changed picture"]
    assert "CTA_PROTECTED" in codes(validate_plan(
        script, groups, plan, protected_cta_beats={"1A": before}
    ))


def test_cta_exemption_from_70_character_split():
    script, groups, plan = sample()
    beats = plan["blocks"][0]["beats"]
    beats[0]["text"] = "A" * 70
    beats[0]["cta_group_id"] = 1
    beats[0]["cta_reference_sources"] = ["ebook"]
    beats[0]["reference"] = True
    script = "".join(b["text"] for b in beats)
    groups[0]["span"]["first_words"] = "A" * 70
    result = validate_plan(script, groups, plan)
    assert "LONG_IMAGE_SPLIT" not in codes(result)
    assert "CTA_REFERENCES" not in codes(result)


def test_workspace_revalidates_before_delivery():
    script, groups, plan = sample()
    workspace = ProfessionalPlanWorkspace(script)
    assert workspace.submit_groups(groups)["status"] == "passed"
    assert workspace.submit_plan_revision(plan)["status"] == "passed"
    assert workspace.deliver()["visual_plan"]["visual_style"] == "profesional"
    workspace.candidate["blocks"][0]["beats"][0]["text"] = "Texto alterado. "
    with pytest.raises(ValueError, match="not green"):
        workspace.deliver()
