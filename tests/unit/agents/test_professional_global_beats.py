"""Global B1.2 semantic cuts: source exactness, 31 overlong beats and patch safety."""
from __future__ import annotations

import pytest

from ai_video_factory.agents.professional.beats import (
    BeatCut,
    GroupCuts,
    WholeScriptCuts,
    combine_cut_corrections,
    validate_and_materialize_cuts,
)
from ai_video_factory.agents.professional.visuals import (
    VisualChoice,
    VisualCorrection,
    VisualGroup,
    WholeVideoVisuals,
    apply_correction,
    assemble_plan,
)


def _groups_and_cuts(count: int = 31):
    first = "El inicio desarrolla una afirmación clara y completa sobre el contexto. "
    second = "La consecuencia aporta un resultado independiente para entender el hecho. "
    assert len(first.strip()) <= 100
    assert len(second.strip()) <= 100
    assert len((first + second).strip()) > 100
    groups = [{
        "block_id": i, "text": first + second,
        "type": "development", "emotional_entry": "intriga",
        "emotional_exit": "comprensión",
    } for i in range(1, count + 1)]
    bad = WholeScriptCuts(blocks=[
        GroupCuts(block_id=i, cuts=[
            BeatCut(end_anchor=second.strip(), occurrence=1, beat_type="claim"),
        ]) for i in range(1, count + 1)
    ])
    good = WholeScriptCuts(blocks=[
        GroupCuts(block_id=i, cuts=[
            BeatCut(end_anchor=first.strip(), occurrence=1, beat_type="claim"),
            BeatCut(end_anchor=second.strip(), occurrence=1, beat_type="consequence"),
        ]) for i in range(1, count + 1)
    ])
    return groups, bad, good


def test_thirty_one_oversized_beats_rejected_before_visuals():
    groups, bad, good = _groups_and_cuts()
    report = validate_and_materialize_cuts(groups, bad)
    assert report["status"] == "blocked"
    assert len([x for x in report["issues"] if x["rule_id"] == "BEAT_LENGTH"]) == 31
    assert report["total_beats"] == 31
    valid = validate_and_materialize_cuts(groups, good)
    assert valid["status"] == "passed"
    assert valid["total_beats"] == 62
    expected = "".join(x["text"] for x in groups)
    actual = "".join(
        beat["text"] for block in valid["blocks"] for beat in block["beats"]
    )
    assert actual == expected
    assert all(
        len(beat["text"].strip()) <= 100
        and len(beat["text"].split()) <= 40
        for block in valid["blocks"] for beat in block["beats"]
    )


def test_model_decides_cuts_and_python_only_materializes():
    groups, _bad, good = _groups_and_cuts(2)
    result = validate_and_materialize_cuts(groups, good)
    assert result["status"] == "passed"
    assert [b["beat_id"] for b in result["blocks"][0]["beats"]] == ["1A", "1B"]
    assert [b["beat_id"] for b in result["blocks"][1]["beats"]] == ["2A", "2B"]
    assert result["blocks"][0]["beats"][0]["text"].endswith("contexto. ")
    assert result["blocks"][0]["beats"][1]["text"].endswith("hecho. ")
    assert all("visual_type" not in beat
               for block in result["blocks"] for beat in block["beats"])


def test_invalid_anchor_and_duplicate_replacement_fail_closed():
    groups, _bad, good = _groups_and_cuts(2)
    corrupted = good.model_copy(deep=True)
    corrupted.blocks[0].cuts[0].end_anchor = "NOT IN SOURCE"
    result = validate_and_materialize_cuts(groups, corrupted)
    assert result["status"] == "blocked"
    assert "CUT_ANCHOR" in {x["rule_id"] for x in result["issues"]}
    with pytest.raises(ValueError, match="Duplicate group IDs"):
        combine_cut_corrections(good, [good.blocks[0], good.blocks[0]])


def _choice(beat_id: str, *, prompt: str) -> VisualChoice:
    return VisualChoice(
        beat_id=beat_id, visual_type="media_image",
        prompt=None, video_prompt=None,
        reference=False, audio=False, image_prompts=[prompt],
        generation_mode="independent",
        editor_notes=[], cta_group_id=None, cta_reference_sources=None,
    )


def test_visual_corrections_patch_only_named_beat_without_reprinting_source():
    groups, _bad, good = _groups_and_cuts(1)
    source = validate_and_materialize_cuts(groups, good)["blocks"]
    initial = WholeVideoVisuals(blocks=[VisualGroup(
        block_id=1, beats=[
            _choice("1A", prompt="Meaningful scene 1"),
            _choice("1B", prompt="Meaningful scene 2"),
        ],
    )])
    plan = assemble_plan(
        narrative_core={"central_question": "Why?", "final_answer": "Result"},
        source_beats=source, visuals=initial,
    )
    before_source = "".join(x["text"] for x in plan["blocks"][0]["beats"])
    patch = VisualCorrection(
        resegmented_blocks=[], replanned_blocks=[],
        visual_patches=[_choice("1B", prompt="A stronger visual")],
    )
    corrected = apply_correction(
        current_plan=plan, source_beats=source, correction=patch,
    )
    assert corrected["blocks"][0]["beats"][0]["image_prompts"] == [
        "Meaningful scene 1"
    ]
    assert corrected["blocks"][0]["beats"][1]["image_prompts"] == [
        "A stronger visual"
    ]
    assert "".join(x["text"] for x in corrected["blocks"][0]["beats"]) == before_source


def test_unrequested_resegmentation_cannot_fabricate_beats():
    groups, _bad, good = _groups_and_cuts(1)
    source = validate_and_materialize_cuts(groups, good)["blocks"]
    plan = assemble_plan(
        narrative_core={"central_question": "Why?", "final_answer": "Result"},
        source_beats=source, visuals=WholeVideoVisuals(blocks=[
            VisualGroup(block_id=1, beats=[
                _choice("1A", prompt="Scene 1"), _choice("1B", prompt="Scene 2"),
            ]),
        ]),
    )
    with pytest.raises(ValueError, match="Every resegmented group"):
        apply_correction(
            current_plan=plan, source_beats=source,
            correction=VisualCorrection(
                resegmented_blocks=good.blocks,
                replanned_blocks=[],
                visual_patches=[],
            ),
        )
