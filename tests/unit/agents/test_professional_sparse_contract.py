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
        "image_prompts", "generation_mode",
    }
    assert set(beats[1]) == {
        "beat_id", "text", "beat_type", "visual_type",
    }
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
    assert result["plan"] is None


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
    assert beat["editor_notes"][0]["trigger"] == {
        "text": "QR code", "occurrence": 1,
    }
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

    bad_notes = _direction(
        _beat("1A", source[0]["text"]),
        notes=[BeatNote(
            beat_id="1Z", kind="overlay", instruction="Missing ID", trigger=None,
        )],
    )
    result = materialize_sparse_direction(source, bad_notes, _core())
    assert result["status"] == "blocked"
    assert "EDITOR_NOTE_BEAT" in {issue["rule_id"] for issue in result["issues"]}


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
