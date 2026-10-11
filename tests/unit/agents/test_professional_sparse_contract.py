"""Regression tests for the source-text-first sparse OpenAI wire contract."""
from __future__ import annotations

import json

import pytest

from ai_video_factory.agents.professional.compact import (
    AvatarBeat,
    BeatNote,
    CTAAssignment,
    ImageBeat,
    ImagePatch,
    NoteTrigger,
    SparseCorrection,
    SparseDirection,
    SparseGroup,
    apply_sparse_correction,
    materialize_sparse_direction,
)
from ai_video_factory.agents.professional.validation import validate_plan


def _core():
    return {"central_question": "Who called?", "final_answer": "Call back."}


def _group(text):
    return [{"block_id": 1, "text": text}]


def _beat(ident, text, *, kind="image"):
    if kind == "avatar":
        return AvatarBeat(
            beat_id=ident, text=text, beat_type="claim", visual_type="avatar",
        )
    return ImageBeat(
        beat_id=ident, text=text, beat_type="claim", visual_type="media_image",
        image_prompts=["An exact, source-grounded supporting image"],
        generation_mode="independent",
    )


def _direction(*beats, notes=None, references=None, cta=None):
    return SparseDirection(blocks=[SparseGroup(
        block_id=1, beats=list(beats),
        editor_notes=notes or [],
        presenter_references=references or [],
        cta=cta or [],
    )])


def test_wire_has_no_unused_beat_fields_or_anchors():
    direction = _direction(
        _beat("1A", "First part."),
        _beat("1B", "Second part.", kind="avatar"),
    )
    beats = direction.model_dump(mode="json")["blocks"][0]["beats"]
    assert set(beats[0]) == {
        "beat_id", "text", "beat_type", "visual_type",
        "editor_notes", "image_prompts", "generation_mode",
    }
    assert set(beats[1]) == {
        "beat_id", "text", "beat_type", "visual_type", "editor_notes",
    }
    assert "editor_notes" not in direction.model_dump(mode="json")["blocks"][0]
    assert all(None not in beat.values() for beat in beats)
    assert all("end_anchor" not in beat and "occurrence" not in beat for beat in beats)
    schema = SparseDirection.model_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "anyOf" in schema["$defs"]["SparseGroup"]["properties"]["beats"]["items"]


def test_source_text_restores_original_line_endings_without_rewriting_words():
    raw = "First event.\r\n  Second event."
    direction = _direction(
        _beat("1A", "First event."),
        _beat("1B", "Second event.", kind="avatar"),
    )
    result = materialize_sparse_direction(_group(raw), direction, _core())
    assert result["status"] == "passed", result["issues"]
    output = result["plan"]["blocks"][0]["beats"]
    assert [beat["text"] for beat in output] == [
        "First event.\r\n  ", "Second event.",
    ]
    assert "".join(beat["text"] for beat in output) == raw
    assert output[1]["visual_type"] == "avatar"
    assert output[1]["audio"] is True


def test_model_cannot_invent_or_reorder_spoken_words():
    result = materialize_sparse_direction(
        _group("First event. Second event."),
        _direction(
            _beat("1A", "First event."),
            _beat("1B", "Completely different event.", kind="avatar"),
        ),
        _core(),
    )
    assert result["status"] == "blocked"
    assert result["plan"] is None
    assert "SOURCE_TEXT" in {issue["rule_id"] for issue in result["issues"]}


def test_length_limit_is_still_hard_and_source_complete():
    first = "A meaningful independent first event with several details. "
    second = "A second independently representable and genuinely complete event."
    assert len((first + second).strip()) > 100
    result = materialize_sparse_direction(
        _group(first + second),
        _direction(_beat("1A", first + second)),
        _core(),
    )
    assert result["status"] == "blocked"
    assert "BEAT_LENGTH" in {issue["rule_id"] for issue in result["issues"]}
    assert result["plan"] is not None
    assert result["plan"]["blocks"][0]["beats"][0]["text"] == first + second


def test_sidecar_notes_references_and_cta_materialize_without_nulls_on_beats():
    source = "Scan the QR code now."
    direction = _direction(
        _beat("1A", source),
        notes=[BeatNote(
            beat_id="1A", kind="overlay", instruction="Show the QR",
            trigger=NoteTrigger(text="QR code", occurrence=1),
        )],
        cta=[CTAAssignment(
            beat_id="1A", cta_group_id=1, sources=["ebook"],
        )],
    )
    result = materialize_sparse_direction(_group(source), direction, _core())
    assert result["status"] == "passed", result["issues"]
    beat = result["plan"]["blocks"][0]["beats"][0]
    assert beat["reference"] is True
    assert beat["cta_reference_sources"] == ["ebook"]
    assert beat["editor_notes"] == "Show the QR"
    wire = json.dumps(direction.model_dump(mode="json"))
    assert '"prompt": null' not in wire


def test_sparse_visual_patch_changes_only_visual_not_narration():
    original = _direction(
        _beat("1A", "Call the bank."),
        _beat("1B", "Confirm it.", kind="avatar"),
    )
    corrected = apply_sparse_correction(
        original, SparseCorrection(
            replaced_groups=[],
            visual_patches=[ImagePatch(
                beat_id="1A", visual_type="media_image",
                image_prompts=["Actual new bank card close-up"],
                generation_mode="independent",
            )],
        ),
    )
    assert corrected.blocks[0].beats[0].text == original.blocks[0].beats[0].text
    assert corrected.blocks[0].beats[1] == original.blocks[0].beats[1]
    assert corrected.blocks[0].beats[0].image_prompts == [
        "Actual new bank card close-up",
    ]


def test_repeated_identical_phrases_resolve_without_skipping_or_duplication():
    source = "No. No. No."
    result = materialize_sparse_direction(
        _group(source), _direction(
            _beat("1A", "No."),
            _beat("1B", "No."),
            _beat("1C", "No.", kind="avatar"),
        ), _core(),
    )
    assert result["status"] == "passed", result["issues"]
    assert [beat["text"] for beat in result["plan"]["blocks"][0]["beats"]] == [
        "No. ", "No. ", "No.",
    ]


def test_source_exact_but_mid_word_split_is_rejected():
    result = materialize_sparse_direction(
        _group("Pague hoy mismo."),
        _direction(
            _beat("1A", "Pa"),
            _beat("1B", "gue hoy mismo.", kind="avatar"),
        ),
        _core(),
    )
    assert result["status"] == "blocked"
    assert result["plan"] is None
    assert "CUT_INSIDE_WORD" in {issue["rule_id"] for issue in result["issues"]}


def test_missing_or_extra_source_blocks_never_produce_a_plan():
    source = _group("First event.")
    direction = _direction(_beat("1A", "First event.", kind="avatar"))
    for missing in ([], source + [{"block_id": 2, "text": "Second event."}]):
        result = materialize_sparse_direction(missing, direction, _core())
        assert result["status"] == "blocked"
        assert result["plan"] is None
        assert "CUT_BLOCKS" in {issue["rule_id"] for issue in result["issues"]}


def test_duplicate_cta_assignment_and_unknown_note_block_delivery():
    source = _group("Scan the QR code now.")
    owner = CTAAssignment(
        beat_id="1A", cta_group_id=1, sources=["ebook"],
    )
    duplicated = _direction(_beat("1A", source[0]["text"]), cta=[owner, owner])
    result = materialize_sparse_direction(source, duplicated, _core())
    assert result["status"] == "blocked"
    assert "CTA_BEAT" in {issue["rule_id"] for issue in result["issues"]}

    # Historical malformed sidecars are rejected while parsing their old
    # group annotation, before a new plan can be materialized.
    with pytest.raises(ValueError, match="EDITOR_NOTE_BEAT"):
        _direction(_beat("1A", source[0]["text"]),
                   notes=[BeatNote(beat_id="1Z", kind="overlay",
                                   instruction="Missing ID", trigger=None)])


def test_duplicate_original_beat_ids_reject_ambiguous_visual_patch():
    original = SparseDirection(blocks=[
        SparseGroup(
            block_id=1, beats=[_beat("1A", "First.")],
            editor_notes=[], presenter_references=[], cta=[],
        ),
        SparseGroup(
            block_id=2, beats=[_beat("1A", "Second.")],
            editor_notes=[], presenter_references=[], cta=[],
        ),
    ])
    with pytest.raises(ValueError, match="Duplicate original beat IDs"):
        apply_sparse_correction(
            original, SparseCorrection(replaced_groups=[], visual_patches=[]),
        )


def test_existing_cover_does_not_authorize_invented_cta():
    script = "Always verify unexpected requests."
    groups = [{
        "block_id": 1, "type": "development",
        "emotional_entry": "concern", "emotional_exit": "clarity",
        "span": {
            "first_words": script,
            "last_words": script,
        },
    }]
    direction = _direction(
        _beat("1A", script),
        cta=[CTAAssignment(
            beat_id="1A", cta_group_id=1, sources=["ebook"],
        )],
    )
    materialized = materialize_sparse_direction(_group(script), direction, _core())
    assert materialized["status"] == "passed", materialized["issues"]
    report = validate_plan(
        script, groups, materialized["plan"], ebook_available=True,
    )
    assert report["status"] == "blocked"
    assert "CTA_UNGROUNDED" in {issue["rule_id"] for issue in report["issues"]}


def test_length_failure_also_reveals_avatar_and_authentic_cta_errors():
    """Python must disclose every checkable fault in one expensive model round."""
    script = (
        "This ebook explains how to recognize a bank scam. "
        "Scan the QR code to get the real guide and protect your family. "
        "Keep these instructions close for the next unexpected call."
    )
    meta = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "concern", "emotional_exit": "clarity",
        "span": {"first_words": script, "last_words": script},
    }]
    direction = _direction(_beat("1A", script, kind="avatar"))
    material = materialize_sparse_direction(_group(script), direction, _core())
    assert material["plan"] is not None
    assert material["status"] == "blocked"
    report = validate_plan(
        script, meta, material["plan"], ebook_available=True,
    )
    codes = {issue["rule_id"] for issue in report["issues"]}
    assert {"BEAT_LENGTH", "AVATAR_RATIO", "SPEAKING_RUN",
            "CTA_SPOKEN_ASSET_UNPROTECTED",
            "CTA_SPOKEN_OFFER_MISSING"}.issubset(codes), codes


def test_full_plan_succeeds_only_with_source_and_visual_validation():
    script = "A phone rings. The recipient looks at it."
    meta = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "concern", "emotional_exit": "clarity",
        "span": {"first_words": script, "last_words": script},
    }]
    direction = _direction(
        _beat("1A", "A phone rings."),
        _beat("1B", "The recipient looks at it.", kind="avatar"),
    )
    material = materialize_sparse_direction(_group(script), direction, _core())
    assert material["status"] == "passed"
    assert material["plan"] is not None
    report = validate_plan(script, meta, material["plan"])
    assert report["status"] == "blocked"
    assert "AVATAR_RATIO" in {issue["rule_id"] for issue in report["issues"]}


def test_professional_connected_visual_sequence_and_anchored_overlay_validate():
    """Professional supports a whole coherent grid scene followed by presenter."""
    parts = [
        "The phone rings. ",
        "A woman notices the call. ",
        "She checks the display. ",
        "She picks up her card. ",
        "She examines the printed number. ",
        "She compares the two. ",
        "The apparent match proves nothing.",
    ]
    script = "".join(parts)
    meta = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "concern", "emotional_exit": "clarity",
        "span": {"first_words": parts[0].strip(),
                 "last_words": parts[-1].strip()},
    }]
    scene = [
        ImageBeat(
            beat_id=f"1{chr(65 + index)}", text=text,
            beat_type="action", visual_type="media_image",
            image_prompts=[f"Continuity frame {index + 1}: actor and object state"],
            generation_mode="grid",
        )
        for index, text in enumerate(parts[:-1])
    ]
    direction = _direction(
        *scene, _beat("1G", parts[-1], kind="avatar"),
        notes=[BeatNote(
            beat_id="1G", kind="overlay",
            instruction="State the concise source-supported final warning.",
            trigger=NoteTrigger(text="proves nothing.", occurrence=1),
        )],
    )
    material = materialize_sparse_direction(_group(script), direction, _core())
    assert material["status"] == "passed", material["issues"]
    report = validate_plan(script, meta, material["plan"])
    assert report["status"] == "passed", report["issues"]
    beats = material["plan"]["blocks"][0]["beats"]
    assert [beat.get("generation_mode") for beat in beats[:6]] == ["grid"] * 6
    assert beats[6]["visual_type"] == "avatar"
    assert beats[6]["editor_notes"] == "State the concise source-supported final warning."
