"""Whole-video audio+visual editorial decisions must be inseparable."""
from __future__ import annotations

import copy

import pytest

from ai_video_factory.agents.professional.direction import (
    DirectionCorrection,
    DirectedBeat,
    DirectedGroup,
    WholeVideoDirection,
    apply_direction_correction,
    materialize_direction,
)
from ai_video_factory.agents.professional.visuals import VisualChoice


def _source():
    first = "El personaje recibe una llamada que parece venir de su banco. "
    second = "Compara atentamente la identidad con su tarjeta de pago. "
    final = "Entonces llama al banco por su cuenta."
    assert len(first.strip()) <= 100 and len(second.strip()) <= 100
    assert len((first + second).strip()) > 100
    return [first, second, final]


def _direction(*, oversized=False, broken_anchor=False):
    parts = _source()
    cut_texts = [parts[1], parts[2]] if oversized else parts
    planned = []
    for i, segment in enumerate(cut_texts):
        close = i == len(cut_texts) - 1
        planned.append(DirectedBeat(
            beat_id=f"1{chr(65 + i)}",
            end_anchor=(
                "Missing ending phrase." if broken_anchor and i == 0
                else segment.strip()
            ),
            occurrence=1, beat_type="return" if close else "action",
            visual_type="avatar" if close else "media_image",
            prompt=None, video_prompt=None,
            reference=False, audio=close,
            image_prompts=None if close else [f"Filming the original physical action {i}"],
            generation_mode=None if close else "independent",
            editor_notes=[], cta_group_id=None,
            cta_reference_sources=None,
        ))
    return WholeVideoDirection(blocks=[
        DirectedGroup(block_id=1, beats=planned),
    ])


def _groups():
    text = "".join(_source())
    return [{
        "block_id": 1, "text": text,
        "type": "close", "emotional_entry": "tension",
        "emotional_exit": "clarity",
    }], {
        "central_question": "¿Quién llama?",
        "final_answer": "Verifica con una llamada nueva.",
    }


def test_joint_model_response_materializes_source_beats_and_visuals_together():
    groups, core = _groups()
    direction = _direction()
    result = materialize_direction(groups, direction, core)
    assert result["status"] == "passed", result["issues"]
    assert result["plan"] is not None
    beats = result["plan"]["blocks"][0]["beats"]
    assert "".join(b["text"] for b in beats) == groups[0]["text"]
    assert all("visual_type" in beat and "beat_type" in beat for beat in beats)
    assert all("prompt" in beat for beat in beats)
    assert [beat["beat_id"] for beat in beats] == ["1A", "1B", "1C"]


def test_an_oversized_beat_still_has_its_complete_visual_direction():
    groups, core = _groups()
    submitted = _direction(oversized=True)
    report = materialize_direction(groups, submitted, core)
    assert report["status"] == "blocked"
    assert "BEAT_LENGTH" in {x["rule_id"] for x in report["issues"]}
    assert report["plan"] is None
    first = report["direction"]["blocks"][0]["beats"][0]
    assert first["visual_type"] == "media_image"
    assert first["image_prompts"]
    assert first["end_anchor"]
    fixed = apply_direction_correction(
        submitted,
        DirectionCorrection(
            replaced_groups=_direction().blocks,
            visual_patches=[],
        ),
    )
    result = materialize_direction(groups, fixed, core)
    assert result["status"] == "passed"
    assert [beat["beat_id"] for beat in result["plan"]["blocks"][0]["beats"]] == [
        "1A", "1B", "1C",
    ]


def test_invalid_anchor_is_repaired_together_with_visuals():
    groups, core = _groups()
    invalid = _direction(broken_anchor=True)
    outcome = materialize_direction(groups, invalid, core)
    assert outcome["status"] == "blocked"
    assert "CUT_ANCHOR" in {x["rule_id"] for x in outcome["issues"]}
    assert outcome["direction"]["blocks"][0]["beats"][0]["image_prompts"]
    fixed = apply_direction_correction(
        invalid, DirectionCorrection(
            replaced_groups=_direction().blocks, visual_patches=[],
        ),
    )
    assert materialize_direction(groups, fixed, core)["status"] == "passed"


def test_visual_patch_preserves_immutable_cut_and_type():
    original = _direction()
    source = copy.deepcopy(original.blocks[0].beats[0])
    fields = {
        key: value for key, value in source.model_dump(mode="json").items()
        if key in VisualChoice.model_fields
    }
    fields["image_prompts"] = ["One coherent physical action and source props"]
    patched = apply_direction_correction(
        original, DirectionCorrection(
            replaced_groups=[],
            visual_patches=[VisualChoice.model_validate(fields)],
        ),
    )
    before = original.blocks[0].beats[0]
    after = patched.blocks[0].beats[0]
    assert before.end_anchor == after.end_anchor
    assert before.beat_type == after.beat_type
    assert before.beat_id == after.beat_id
    assert after.image_prompts == ["One coherent physical action and source props"]
    assert patched.blocks[0].beats[1] == original.blocks[0].beats[1]


def test_cannot_mix_group_replacement_with_independent_visual_patch():
    original = _direction()
    old = original.blocks[0].beats[0]
    patch = VisualChoice.model_validate({
        key: value for key, value in old.model_dump(mode="json").items()
        if key in VisualChoice.model_fields
    })
    with pytest.raises(ValueError, match="already fully replaced"):
        apply_direction_correction(
            original,
            DirectionCorrection(
                replaced_groups=_direction().blocks,
                visual_patches=[patch],
            ),
        )


def test_unknown_replacement_group_and_duplicate_visual_patch_fail_closed():
    original = _direction()
    invalid = DirectedGroup(block_id=99, beats=_direction().blocks[0].beats)
    with pytest.raises(ValueError, match="unknown narrative group"):
        apply_direction_correction(original, DirectionCorrection(
            replaced_groups=[invalid], visual_patches=[],
        ))
    first = original.blocks[0].beats[0]
    patch = VisualChoice.model_validate({
        key: value for key, value in first.model_dump(mode="json").items()
        if key in VisualChoice.model_fields
    })
    with pytest.raises(ValueError, match="Duplicate visual correction"):
        apply_direction_correction(original, DirectionCorrection(
            replaced_groups=[], visual_patches=[patch, patch],
        ))
