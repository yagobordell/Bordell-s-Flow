"""Offline end-to-end proof: ONE joint beat+visual model pass per whole video."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from ai_video_factory.agents.professional.compact import (
    SparseCorrection as DirectionCorrection,
    SparseDirection as WholeVideoDirection,
    SparseGroup, ImageBeat, AvatarBeat, ImagePatch, CTAAssignment,
)
from ai_video_factory.agents.professional.feedback import (
    approve_feedback,
    parse_feedback_arg,
)
from ai_video_factory.agents.professional.runner import (
    CreativeReview,
    GroupProposal,
    run_professional_director,
)


class FakeAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def clone(self, **kwargs):
        return FakeAgent(**{**vars(self), **kwargs})


class FakeModelBehaviorError(Exception):
    pass


def _fixture():
    parts = [
        "Una persona recibe una llamada inesperada en su telefono. ",
        "Consulta atentamente los datos antes de tomar la decision. ",
        "Guarda la tarjeta y verifica el nombre con calma. ",
        "La conclusion llega cuando vuelve a llamar por su cuenta.",
    ]
    script = "".join(parts)
    groups = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "intriga", "emotional_exit": "claridad",
        "span": {
            "first_words": parts[0].strip(),
            "last_words": parts[-1].strip(),
        },
    }]
    core = {
        "central_question": "¿Quién llama?",
        "final_answer": "Verifica de forma independiente.",
    }
    return script, groups, core, parts


def _direction(parts, *, long_first=False, bad_anchor=False,
               invented_ebook_cta=False, richer=False):
    slices = ([parts[0] + parts[1], parts[2], parts[3]]
              if long_first else parts)
    beats = []
    for i, part in enumerate(slices):
        final = i == len(slices) - 1
        beat_id = f"1{chr(65 + i)}"
        text = ("This phrase is absent from the original source."
                if bad_anchor and i == 0 else part)
        if final:
            beat = AvatarBeat(
                beat_id=beat_id, text=text,
                beat_type="return", visual_type="avatar",
            )
        else:
            beat = ImageBeat(
                beat_id=beat_id, text=text,
                beat_type="claim", visual_type="media_image",
                image_prompts=[
                    ("An even more grounded full scene" if richer and i == 1
                     else f"Authentic distinct full-frame still {i + 1}")
                ],
                generation_mode="independent",
            )
        beats.append(beat)
    return WholeVideoDirection(blocks=[SparseGroup(
        block_id=1, beats=beats, editor_notes=[],
        presenter_references=[],
        cta=([CTAAssignment(
            beat_id="1A", cta_group_id=1, sources=["ebook"],
        )] if invented_ebook_cta else []),
    )])


@pytest.mark.parametrize("bad_json", [False, True])
@pytest.mark.parametrize("bad_cut", [False, True])
@pytest.mark.parametrize("missing_anchor", [False, True])
@pytest.mark.parametrize("bad_cta", [False, True])
@pytest.mark.parametrize("creative_patch", [False, True])
def test_director_one_joint_plan_and_global_correction(
    tmp_path, monkeypatch, bad_json, bad_cut, missing_anchor,
    bad_cta, creative_patch,
):
    script, groups, core, parts = _fixture()
    outputs = []
    malformed_used = False
    need_correction = bad_cut or missing_anchor or bad_cta or creative_patch

    class FakeRunner:
        @staticmethod
        async def run(agent, payload, *, max_turns=1, hooks=None):
            nonlocal malformed_used
            outputs.append((agent.output_type, payload, agent))
            assert max_turns == 1
            assert agent.tools == []
            assert not hasattr(agent, "model_settings")
            if agent.output_type is GroupProposal:
                answer = GroupProposal.model_validate({
                    "narrative_core": core, "blocks": groups,
                })
            elif agent.output_type is WholeVideoDirection:
                answer = _direction(
                    parts, long_first=bad_cut, bad_anchor=missing_anchor,
                    invented_ebook_cta=bad_cta,
                )
            elif agent.output_type is CreativeReview:
                answer = CreativeReview.model_validate({
                    "whole_video_assessment": "Full video inspected",
                    "issues": [{
                        "beat_id": "1B",
                        "problem": "The visual choice lacks causal staging",
                        "correction": "Improve the physical action of the scene",
                    }] if creative_patch else [],
                })
            elif agent.output_type is DirectionCorrection:
                replace = bad_cut or missing_anchor or bad_cta
                patches = []
                if not replace and creative_patch:
                    replacement = _direction(parts, richer=True).blocks[0].beats[1]
                    patches.append(ImagePatch(
                        beat_id=replacement.beat_id, visual_type="media_image",
                        image_prompts=replacement.image_prompts,
                        generation_mode="independent",
                    ))
                answer = DirectionCorrection(
                    replaced_groups=(
                        _direction(parts, richer=creative_patch).blocks if replace else []
                    ),
                    visual_patches=patches,
                )
            else:
                raise AssertionError("Unexpected phase: " + str(agent.output_type))
            usage = SimpleNamespace(
                requests=1, input_tokens=120, output_tokens=33,
                total_tokens=153,
                input_tokens_details=SimpleNamespace(cached_tokens=4),
            )
            if hooks:
                await hooks.on_llm_end(
                    SimpleNamespace(usage=usage), agent,
                    SimpleNamespace(usage=usage, response_id="offline"),
                )
            if bad_json and not malformed_used:
                malformed_used = True
                raise FakeModelBehaviorError("Invalid JSON")
            return SimpleNamespace(
                final_output=answer,
                context_wrapper=SimpleNamespace(usage=usage),
            )

    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        Agent=FakeAgent, Runner=FakeRunner,
        ModelBehaviorError=FakeModelBehaviorError,
        RunHooks=type("RunHooks", (), {}),
        set_default_openai_key=lambda key: None,
    ))
    result = asyncio.run(run_professional_director(
        script, output=tmp_path, avatar=b"PNG", ebook=None, model="offline",
    ))
    assert result["validation"]["status"] == "passed"
    phases = [kind for kind, _payload, _agent in outputs]
    assert phases.count(WholeVideoDirection) == 1
    assert phases.count(CreativeReview) == 1
    assert phases.count(DirectionCorrection) == int(need_correction)
    assert len(outputs) == 3 + int(bad_json) + int(need_correction)
    all_phases = tmp_path / "agent_openai" / "phases"
    assert sorted(p.name for p in all_phases.iterdir() if p.is_dir()) == [
        "01_groups", "02_planning", "03_evaluation",
        "04_correction", "05_delivery",
    ]
    phase2 = json.loads(
        (all_phases / "02_planning" / "output.json").read_text(encoding="utf-8")
    )
    assert phase2["output"]["blocks"][0]["beats"][0]["visual_type"] == "media_image"
    first = phase2["output"]["blocks"][0]["beats"][0]
    assert first["text"] == (
        parts[0] + parts[1] if bad_cut else (
            "This phrase is absent from the original source."
            if missing_anchor else parts[0]
        )
    )
    assert "end_anchor" not in first
    assert "occurrence" not in first
    assert "prompt" not in first
    assert "video_prompt" not in first
    assert first["image_prompts"]
    assert "materialized_plan" not in phase2
    assert "blocks" not in phase2["boundary_validation"]
    assert all(
        beat["text"] is not None
        for block in phase2["output"]["blocks"] for beat in block["beats"]
    )
    assert not (all_phases / "02_planning" / "beats").exists()
    assert not (all_phases / "02_planning" / "visuals").exists()
    assert (all_phases / "03_evaluation" / "output.json").is_file()
    assert (all_phases / "04_correction" / "output.json").is_file()
    assert (all_phases / "05_delivery" / "output.json").is_file()
    for location in [
        "B1.1/output.json", "B1.2/block_1/output.json",
        "B1.2/merged_output.json", "B2/block_1/output.json",
        "B2/merged_output.json", "visual_plan.json",
    ]:
        assert (tmp_path / location).is_file(), location
    approved = json.loads(
        (tmp_path / "agent_openai" / "approved_plan.json").read_text()
    )
    beats = approved["plan"]["blocks"][0]["beats"]
    assert "".join(b["text"] for b in beats) == script
    assert len(beats) == 4
    assert all(len(b["text"].strip()) <= 100 for b in beats)
    assert not (tmp_path / "audio").exists()
    assert not (tmp_path / "images").exists()
    assert len(result["usage"]) == len(outputs)


def test_rejected_joint_direction_retains_all_visuals_no_premature_b12(
    tmp_path, monkeypatch,
):
    script, groups, core, parts = _fixture()
    kinds = []

    class FakeRunner:
        @staticmethod
        async def run(agent, payload, *, max_turns=1, hooks=None):
            kinds.append(agent.output_type)
            if agent.output_type is GroupProposal:
                answer = GroupProposal.model_validate({
                    "narrative_core": core, "blocks": groups,
                })
            elif agent.output_type is WholeVideoDirection:
                answer = _direction(parts, long_first=True)
            elif agent.output_type is CreativeReview:
                answer = CreativeReview(
                    whole_video_assessment="Narration beat too long",
                    issues=[],
                )
            elif agent.output_type is DirectionCorrection:
                answer = DirectionCorrection(
                    replaced_groups=[], visual_patches=[],
                )
            else:
                raise AssertionError("Unexpected phase")
            usage = SimpleNamespace(
                requests=1, input_tokens=10, output_tokens=10,
                total_tokens=20,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
            )
            if hooks:
                await hooks.on_llm_end(
                    None, agent, SimpleNamespace(usage=usage, response_id="offline"),
                )
            return SimpleNamespace(
                final_output=answer,
                context_wrapper=SimpleNamespace(usage=usage),
            )

    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        Agent=FakeAgent, Runner=FakeRunner,
        ModelBehaviorError=FakeModelBehaviorError,
        RunHooks=type("RunHooks", (), {}),
        set_default_openai_key=lambda key: None,
    ))
    with pytest.raises(ValueError, match="cannot deliver"):
        asyncio.run(run_professional_director(
            script, output=tmp_path, avatar=b"PNG", ebook=None, model="offline",
        ))
    phase2 = json.loads(
        (tmp_path / "agent_openai" / "phases" / "02_planning"
         / "output.json").read_text(encoding="utf-8")
    )
    assert phase2["status"] == "blocked"
    assert phase2["output"]["blocks"][0]["beats"][0]["image_prompts"]
    assert phase2["output"]["blocks"][0]["beats"][0]["text"] == (
        parts[0] + parts[1]
    )
    assert "BEAT_LENGTH" in {
        issue["rule_id"] for issue in phase2["boundary_validation"]["issues"]
    }
    assert (tmp_path / "B1.1" / "output.json").is_file()
    assert not (tmp_path / "B1.2" / "merged_output.json").exists()
    blocked_delivery = json.loads(
        (tmp_path / "agent_openai" / "phases"
         / "05_delivery" / "output.json").read_text(encoding="utf-8")
    )
    assert "original_full_audiovisual_plan" not in blocked_delivery
    assert blocked_delivery["proposed_plan"]["blocks"][0]["beats"][0]["text"]
    assert kinds.count(WholeVideoDirection) == 1
    assert kinds.count(DirectionCorrection) == 1


def test_failure_during_integrated_phase_keeps_groups_but_not_false_beats(
    tmp_path, monkeypatch,
):
    script, groups, core, _parts = _fixture()

    class FakeRunner:
        @staticmethod
        async def run(agent, payload, *, max_turns=1, hooks=None):
            if agent.output_type is GroupProposal:
                result = GroupProposal.model_validate({
                    "narrative_core": core, "blocks": groups,
                })
                usage = SimpleNamespace(
                    requests=1, input_tokens=10, output_tokens=10,
                    total_tokens=20,
                    input_tokens_details=SimpleNamespace(cached_tokens=0),
                )
                return SimpleNamespace(
                    final_output=result,
                    context_wrapper=SimpleNamespace(usage=usage),
                )
            raise RuntimeError("model offline")

    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        Agent=FakeAgent, Runner=FakeRunner,
        ModelBehaviorError=FakeModelBehaviorError,
        RunHooks=type("RunHooks", (), {}),
        set_default_openai_key=lambda key: None,
    ))
    with pytest.raises(RuntimeError, match="02_planning"):
        asyncio.run(run_professional_director(
            script, output=tmp_path, avatar=b"PNG", ebook=None, model="offline",
        ))
    assert (tmp_path / "B1.1" / "output.json").is_file()
    assert not (tmp_path / "B1.2" / "merged_output.json").exists()
    phase2 = json.loads(
        (tmp_path / "agent_openai" / "phases" / "02_planning"
         / "output.json").read_text(encoding="utf-8")
    )
    assert phase2["status"] == "failed"


def test_feedback_accepts_text_or_markdown(tmp_path):
    assert parse_feedback_arg("Menos planos repetidos") == "Menos planos repetidos"
    target = tmp_path / "feedback.md"
    target.write_text("Revisar la transición.", encoding="utf-8")
    assert parse_feedback_arg("@" + str(target)) == "Revisar la transición."
    assert parse_feedback_arg(str(target)) == "Revisar la transición."


def test_approve_feedback_updates_memory_only(tmp_path):
    output = tmp_path / "video"
    folder = output / "agent_openai" / "feedback"
    folder.mkdir(parents=True)
    proposal = folder / "proposal_demo.json"
    proposal.write_text(json.dumps({
        "schema_version": "professional-feedback-v1",
        "status": "pending_approval",
        "script_sha256": "sha",
        "feedback": "Evitar imágenes demasiado parecidas.",
        "analysis": {
            "explanation": "Editorial learning",
            "groups_memory_additions": ["Keep narrative groups coherent."],
            "beats_visuals_memory_additions": ["Avoid duplicate angles."],
            "instruction_clarifications": [{
                "file": "rules/video_motion.md",
                "reason": "Ambiguous camera direction",
                "proposed_text": "Needs human review",
            }],
        },
    }), encoding="utf-8")
    memory = tmp_path / "memory"
    answer = approve_feedback(proposal, output=output, memory_root=memory)
    assert answer["memory_additions"]["groups.md"] == 1
    assert "Avoid duplicate angles." in (memory / "beats_visuals.md").read_text()
    assert not (memory / "video_motion.md").exists()
    assert "approved_memory_only" in proposal.read_text()
    with pytest.raises(ValueError, match="not pending approval"):
        approve_feedback(proposal, output=output, memory_root=memory)


def test_global_correction_retries_only_while_source_errors_improve(
    tmp_path, monkeypatch,
):
    """A 2-error -> 1-error -> green recut stays in one 04 phase."""
    script, groups, core, parts = _fixture()
    seen = []
    correction_calls = 0

    def direction_from_ranges(ranges):
        original = _direction(parts).blocks[0].beats
        beats = []
        for number, (start, end) in enumerate(ranges):
            final = number == len(ranges) - 1
            beat = original[-1 if final else start].model_copy(
                deep=True, update={
                    "beat_id": f"1{chr(65 + number)}",
                    "text": "".join(parts[start:end]),
                },
            )
            beats.append(beat)
        return WholeVideoDirection(blocks=[SparseGroup(
            block_id=1, beats=beats, editor_notes=[],
            presenter_references=[], cta=[],
        )])

    class FakeRunner:
        @staticmethod
        async def run(agent, payload, *, max_turns=1, hooks=None):
            nonlocal correction_calls
            seen.append(agent.output_type)
            if agent.output_type is GroupProposal:
                output = GroupProposal.model_validate({
                    "narrative_core": core, "blocks": groups,
                })
            elif agent.output_type is WholeVideoDirection:
                output = direction_from_ranges([(0, 2), (2, 4)])
            elif agent.output_type is CreativeReview:
                output = CreativeReview(
                    whole_video_assessment="Must fix source lengths",
                    issues=[],
                )
            elif agent.output_type is DirectionCorrection:
                correction_calls += 1
                ranges = (
                    [(0, 2), (2, 3), (3, 4)] if correction_calls == 1
                    else [(0, 1), (1, 2), (2, 3), (3, 4)]
                )
                output = DirectionCorrection(
                    replaced_groups=direction_from_ranges(ranges).blocks,
                    visual_patches=[],
                )
            else:
                raise AssertionError(f"Unexpected type: {agent.output_type}")
            usage = SimpleNamespace(
                requests=1, input_tokens=10, output_tokens=10,
                total_tokens=20,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
            )
            if hooks:
                await hooks.on_llm_end(
                    None, agent, SimpleNamespace(
                        usage=usage, response_id="offline",
                    ),
                )
            return SimpleNamespace(
                final_output=output,
                context_wrapper=SimpleNamespace(usage=usage),
            )

    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        Agent=FakeAgent, Runner=FakeRunner,
        ModelBehaviorError=FakeModelBehaviorError,
        RunHooks=type("RunHooks", (), {}),
        set_default_openai_key=lambda key: None,
    ))
    result = asyncio.run(run_professional_director(
        script, output=tmp_path, avatar=b"PNG", ebook=None, model="offline",
    ))
    assert result["validation"]["status"] == "passed"
    assert correction_calls == 2
    assert seen.count(DirectionCorrection) == 2
    phase04 = tmp_path / "agent_openai" / "phases" / "04_correction"
    assert (phase04 / "attempt_1.json").is_file()
    assert (phase04 / "attempt_2.json").is_file()
    final = json.loads((phase04 / "output.json").read_text(encoding="utf-8"))
    assert final["status"] == "passed"
    assert [item["status"] for item in final["correction_attempts"]] == [
        "blocked", "passed",
    ]
    assert "".join(
        beat["text"]
        for block in final["proposed_plan"]["blocks"]
        for beat in block["beats"]
    ) == script
