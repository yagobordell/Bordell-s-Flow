"""Unified Agent B output, narrative integrity, global sequences and safe checkpoints."""

import pytest

from ai_video_factory.bots.agent_b import (
    AgentBProject,
    PlanRejected,
    ProjectIdentityMismatch,
    StaleRevision,
    sequence_index,
    validate_unified_plan,
)
from ai_video_factory.bots.contracts import MaterializedBlock

CORE = {"central_question": "What is this call?", "final_answer": "A spoofed number."}


def _beat(beat_id, text, *, prompt=None, visual_type="media_image", audio=False,
          subshots=None, **extra):
    if prompt is None and visual_type != "avatar":
        prompt = "A relevant documentary photograph."
    beat = {"beat_id": beat_id, "text": text, "beat_type": "action",
            "visual_type": visual_type, "prompt": prompt, "video_prompt": None,
            "reference": False, "audio": audio, **extra}
    if subshots is not None:
        beat["visual_subshots"] = subshots
    return beat


def _source(block_id, text, type="intro"):
    return MaterializedBlock(block_id=block_id, type=type, emotional_entry="uncertain",
                             emotional_exit="revealed", text=text)


def _plan(*entries):
    return {"schema_version": "b-pipeline-v1", "visual_style": "profesional",
            "narrative_core": CORE,
            "blocks": [{"block_id": src.block_id, "type": src.type,
                        "emotional_entry": src.emotional_entry,
                        "emotional_exit": src.emotional_exit,
                        "beats": beats} for src, beats in entries]}


def _checks(plan, *sources):
    return [e.code for e in validate_unified_plan(plan, sources, CORE)]


def test_emotional_suspense_cuts_inside_a_sentence_are_valid():
    texts = ["Your phone rings. ", "You look at the screen. ",
             "Chase. The bank’s logo. ", "And the number is the same one ",
             "printed on the back of your debit card. ",
             "You even pick up the card to check. ", "It matches."]
    src = _source(1, "".join(texts))
    beats = [_beat(f"1{chr(65 + i)}", t, prompt=f"A{i+1}. Scene {i+1}.")
             for i, t in enumerate(texts)]
    assert _checks(_plan((src, beats)), src) == []


def test_sequences_resume_after_unrelated_blocks():
    a = _source(1, "Beginning.")
    b = _source(2, "Elsewhere.")
    c = _source(7, "Back to the same person.")
    plan = _plan((a, [_beat("1A", a.text, prompt="A1. Phone on table.")]),
                 (b, [_beat("2A", "Else", prompt="B1. Other room."),
                      _beat("2B", "where.", prompt="B2. Other view.")]),
                 (c, [_beat("7A", c.text, prompt="A2. The same phone and person.")]))
    assert _checks(plan, a, b, c) == []
    assert [entry["label"] for entry in sequence_index(plan)["A"]] == ["A1", "A2"]


def test_rejects_numbered_sequence_out_of_order():
    s = _source(1, "First. Second.")
    plan = _plan((s, [_beat("1A", "First. ", prompt="A2. Later."),
                      _beat("1B", "Second.", prompt="A1. Earlier.")]))
    assert "SEQUENCE_ORDINAL" in _checks(plan, s)


def test_rejects_mutated_source_character():
    s = _source(1, "Wait. What now?")
    p = _plan((s, [_beat("1A", "Wait. "), _beat("1B", "What now!")]))
    assert "SOURCE_TEXT" in _checks(p, s)


def test_speaking_cap_crosses_block_boundaries_and_cta():
    s1, s2 = _source(1, "X" * 90), _source(2, "Y" * 40)
    p = _plan((s1, [_beat("1A", s1.text, visual_type="avatar", audio=True,
                        cta_group_id=1)]),
              (s2, [_beat("2A", s2.text, visual_type="avatar", audio=True)]))
    assert "AVATAR_RUN" in _checks(p, s1, s2)


def test_image_split_uses_raw_length_at_70():
    s = _source(1, "A" * 70)
    plan = _plan((s, [_beat("1A", s.text)]))
    assert "REQUIRED_SUBSHOTS" in _checks(plan, s)
    first = {"asset_id": "1A__1", "subshot_index": 1, "subshot_count": 2,
             "prompt": "A1. The first photograph.", "generation": "grid"}
    second = {"asset_id": "1A__2", "subshot_index": 2, "subshot_count": 2,
              "prompt": "A2. A different frame.", "generation": "grid"}
    plan["blocks"][0]["beats"][0].update(prompt=first["prompt"],
                                         visual_subshots=[first, second])
    assert _checks(plan, s) == []
    assert [e["asset_id"] for e in sequence_index(plan)["A"]] == ["1A__1", "1A__2"]


def test_non_cta_short_beat_may_not_gain_subshots():
    s = _source(1, "Short.")
    plan = _plan((s, [_beat("1A", s.text,
                          subshots=[{"prompt": "A1. A frame."},
                                    {"prompt": "A2. Another."}])]))
    assert "UNEXPECTED_SUBSHOTS" in _checks(plan, s)


def test_cta_70_character_beat_is_exempt_from_split():
    s = _source(1, "A" * 75)
    plan = _plan((s, [_beat("1A", s.text, cta_group_id=1)]))
    assert "REQUIRED_SUBSHOTS" not in _checks(plan, s)


def test_close_anchor_is_frozen():
    s = _source(1, "Goodbye.", type="close")
    p = _plan((s, [_beat("1A", s.text)]))
    assert "CLOSE_ANCHOR" in _checks(p, s)
    p["blocks"][0]["beats"] = [_beat("1A", s.text, visual_type="avatar", audio=True)]
    assert _checks(p, s) == []


def test_project_validates_before_write_and_freezes(tmp_path):
    s = _source(1, "Enough.")
    project = AgentBProject(tmp_path / "one", [s], CORE, instruction_sha256="v1")
    assert project.status()["revision"] == 0
    bad = _plan((s, [_beat("1A", "Altered.")]))
    with pytest.raises(PlanRejected) as exc:
        project.save_visual_plan(bad, expected_revision=0)
    assert any(issue.code == "SOURCE_TEXT" for issue in exc.value.issues)
    assert project.status()["revision"] == 0
    plan = _plan((s, [_beat("1A", s.text)]))
    assert project.save_visual_plan(plan, expected_revision=0) == 1
    with pytest.raises(StaleRevision):
        project.save_visual_plan(plan, expected_revision=0)
    project.freeze(expected_revision=1)
    assert project.frozen_plan() == plan
    with pytest.raises(StaleRevision):
        project.save_visual_plan(plan, expected_revision=2)
    again = AgentBProject(tmp_path / "one", [s], CORE, instruction_sha256="v1")
    assert again.frozen_plan() == plan
    with pytest.raises(ProjectIdentityMismatch):
        AgentBProject(tmp_path / "one", [s], CORE, instruction_sha256="v2")


def test_outside_cta_grid_cannot_share_logical_sequence():
    s = _source(1, "One. Two.")
    p = _plan((s, [_beat("1A", "One. ", prompt="A1. A photo.", cta_group_id=1),
                   _beat("1B", "Two.", prompt="A2. Another photo.")]))
    assert "SEQUENCE_CTA_BOUNDARY" in _checks(p, s)


def test_local_numeric_grids_are_rejected_in_new_plan():
    s = _source(1, "Beginning.")
    p = _plan((s, [_beat("1A", s.text, prompt="1. Previous convention.")]))
    assert "LEGACY_GRID_LABEL" in _checks(p, s)
