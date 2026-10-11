"""Offline exact-source end-to-end tests for the global Professional SDK director."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from ai_video_factory.agents.professional.beats import WholeScriptCuts
from ai_video_factory.agents.professional.feedback import (
    approve_feedback,
    parse_feedback_arg,
)
from ai_video_factory.agents.professional.runner import (
    CreativeReview,
    GroupProposal,
    run_professional_director,
)
from ai_video_factory.agents.professional.visuals import (
    VisualAlignmentRepair,
    VisualCorrection,
    WholeVideoVisuals,
)


class FakeAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def clone(self, **kwargs):
        return FakeAgent(**{**vars(self), **kwargs})


class FakeModelBehaviorError(Exception):
    pass


def _fixture():
    parts = ["Una idea concreta. ", "Otra idea clara. ", "Una tercera prueba. ", "Y cerramos."]
    script = "".join(parts)
    blocks = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "neutro", "emotional_exit": "conclusion",
        "span": {"first_words": "Una idea concreta.", "last_words": "Y cerramos."},
    }]
    core = {"central_question": "¿Qué?", "final_answer": "Una conclusión."}
    cuts = [{"end_anchor": text.strip(), "occurrence": 1, "beat_type": "claim"}
            for text in parts]
    cuts[-1]["beat_type"] = "return"
    choices = [{
        "beat_id": f"1{chr(65 + i)}",
        "visual_type": "media_image",
        "prompt": None, "video_prompt": None,
        "audio": False, "reference": False,
        "image_prompts": [f"Distinct image {i + 1}"],
        "generation_mode": "independent",
        "editor_notes": [],
        "cta_group_id": None, "cta_reference_sources": None,
    } for i in range(len(parts))]
    choices[-1].update({
        "visual_type": "avatar", "image_prompts": None,
        "generation_mode": None, "audio": True,
    })
    return script, blocks, core, cuts, choices


@pytest.mark.parametrize("fail_first_json", [False, True])
@pytest.mark.parametrize("invented_ebook_cta", [False, True])
@pytest.mark.parametrize("editorial_patch", [False, True])
@pytest.mark.parametrize("missing_visual_beat", [False, True])
def test_global_sdk_director_preserves_old_outputs_and_stops_before_media(
    tmp_path, monkeypatch, fail_first_json, invented_ebook_cta,
    editorial_patch, missing_visual_beat,
):
    script, groups, core, cuts, choices = _fixture()
    invocations = []
    requests = 0

    class FakeRunner:
        @staticmethod
        async def run(agent, payload, *, max_turns=1, hooks=None):
            nonlocal requests
            requests += 1
            invocations.append((agent, payload, max_turns))
            assert max_turns == 1
            assert agent.tools == []
            assert not hasattr(agent, "model_settings")
            if agent.output_type is GroupProposal:
                answer = GroupProposal.model_validate({
                    "narrative_core": core, "blocks": groups,
                })
            elif agent.output_type is WholeScriptCuts:
                answer = WholeScriptCuts.model_validate({
                    "blocks": [{"block_id": 1, "cuts": cuts}],
                })
            elif agent.output_type is WholeVideoVisuals:
                selected = copy.deepcopy(choices)
                if invented_ebook_cta:
                    selected[0]["cta_group_id"] = 1
                    selected[0]["cta_reference_sources"] = ["ebook"]
                    selected[0]["reference"] = True
                answer = WholeVideoVisuals.model_validate({
                    "blocks": [{
                        "block_id": 1,
                        "beats": selected[:-1] if missing_visual_beat else selected,
                    }],
                })
            elif agent.output_type is VisualAlignmentRepair:
                selected = copy.deepcopy(choices)
                if invented_ebook_cta:
                    selected[0]["cta_group_id"] = 1
                    selected[0]["cta_reference_sources"] = ["ebook"]
                    selected[0]["reference"] = True
                answer = VisualAlignmentRepair.model_validate({
                    "blocks": [{"block_id": 1, "beats": selected}],
                })
            elif agent.output_type is CreativeReview:
                issues = [{
                    "beat_id": "1B", "problem": "A generic still",
                    "correction": "Use a more concrete scene.",
                }] if editorial_patch else []
                answer = CreativeReview.model_validate({
                    "whole_video_assessment": "Cohesive progression",
                    "issues": issues,
                })
            elif agent.output_type is VisualCorrection:
                chosen = copy.deepcopy(choices)
                changed = []
                if invented_ebook_cta:
                    changed.append(chosen[0])
                if editorial_patch:
                    chosen[1]["image_prompts"] = ["A richer grounded viewpoint"]
                    changed.append(chosen[1])
                answer = VisualCorrection.model_validate({
                    "resegmented_blocks": [],
                    "replanned_blocks": [],
                    "visual_patches": changed,
                })
            else:
                raise AssertionError("Unexpected real-model phase.")
            usage = SimpleNamespace(
                requests=1, input_tokens=120, output_tokens=33,
                total_tokens=153, input_tokens_details=SimpleNamespace(cached_tokens=4),
            )
            if hooks:
                await hooks.on_llm_end(
                    SimpleNamespace(usage=usage), agent,
                    SimpleNamespace(usage=usage, response_id="offline"),
                )
            if fail_first_json and requests == 1:
                raise FakeModelBehaviorError("Invalid JSON")
            return SimpleNamespace(
                final_output=answer, context_wrapper=SimpleNamespace(usage=usage),
            )

    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        Agent=FakeAgent, Runner=FakeRunner,
        ModelBehaviorError=FakeModelBehaviorError,
        RunHooks=type("RunHooks", (), {}),
        set_default_openai_key=lambda key: None,
    ))
    outcome = asyncio.run(run_professional_director(
        script, output=tmp_path, avatar=b"PNG", ebook=None, model="offline",
    ))
    assert outcome["validation"]["status"] == "passed"
    expected = (
        4 + int(fail_first_json) + int(missing_visual_beat)
        + int(invented_ebook_cta or editorial_patch)
    )
    assert len(invocations) == expected
    assert not any(call[0].output_type is str for call in invocations)
    assert invocations[0][0].output_type is GroupProposal
    assert invocations[1 + int(fail_first_json)][0].output_type is WholeScriptCuts
    assert invocations[2 + int(fail_first_json)][0].output_type is WholeVideoVisuals
    assert all(call[2] == 1 for call in invocations)
    assert all(call[0].model == "offline" for call in invocations)
    if invented_ebook_cta:
        initial = json.loads((tmp_path / "agent_openai" / "plan_initial.json").read_text())
        assert "CTA_COVER_UNAVAILABLE" in {
            e["rule_id"] for e in initial["validation"]["issues"]
        }
    assert (tmp_path / "agent_openai" / "phases" / "01_groups" / "output.json").is_file()
    assert (tmp_path / "agent_openai" / "phases" / "02_planning" / "beats" / "output.json").is_file()
    assert (tmp_path / "agent_openai" / "phases" / "02_planning" / "visuals" / "output.json").is_file()
    assert (tmp_path / "agent_openai" / "phases" / "02_planning" / "output.json").is_file()
    assert (tmp_path / "agent_openai" / "phases" / "03_evaluation" / "output.json").is_file()
    assert sorted(
        d.name for d in (tmp_path / "agent_openai" / "phases").iterdir()
        if d.is_dir()
    ) == [
        "01_groups", "02_planning", "03_evaluation",
        "04_correction", "05_delivery",
    ]
    assert (tmp_path / "agent_openai" / "phases" / "05_delivery" / "output.json").is_file()
    assert (tmp_path / "agent_openai" / "phases" / "04_correction" / "output.json").is_file()
    if missing_visual_beat:
        assert (tmp_path / "agent_openai" / "phases" / "02_planning" / "visuals"
                / "repair_1.json").is_file()
    assert (tmp_path / "B1.1" / "output.json").is_file()
    assert (tmp_path / "B1.2" / "block_1" / "output.json").is_file()
    assert (tmp_path / "B1.2" / "merged_output.json").is_file()
    assert (tmp_path / "B2" / "block_1" / "output.json").is_file()
    assert (tmp_path / "B2" / "merged_output.json").is_file()
    assert (tmp_path / "B2.1" / "review_output.json").is_file()
    assert (tmp_path / "visual_plan.json").is_file()
    assert not (tmp_path / "audio").exists()
    assert not (tmp_path / "images").exists()
    approved = json.loads((tmp_path / "agent_openai" / "approved_plan.json").read_text())
    all_beats = [b for g in approved["plan"]["blocks"] for b in g["beats"]]
    assert "".join(b["text"] for b in all_beats) == script
    assert all(len(b["text"].strip()) <= 100 for b in all_beats)
    assert len(outcome["usage"]) == expected
    if fail_first_json:
        assert outcome["usage"][0]["status"] == "invalid_json"
        assert (tmp_path / "agent_openai" / "phases" / "01_groups"
                / "failed_json_1.json").is_file()
    if editorial_patch or invented_ebook_cta:
        assert (tmp_path / "agent_openai" / "phases" / "04_correction"
                / "attempt_1.json").is_file()


def test_missing_ebook_cover_blocks_real_promo_without_spending_model_tokens(
    tmp_path, monkeypatch,
):
    calls = []

    class MustNotBeCalled:
        @staticmethod
        async def run(agent, payload, *, max_turns=1, hooks=None):
            calls.append("paid-request")
            raise AssertionError("Preflight must reject before paid model work")

    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        Agent=FakeAgent, Runner=MustNotBeCalled,
        ModelBehaviorError=FakeModelBehaviorError,
        RunHooks=type("RunHooks", (), {}),
        set_default_openai_key=lambda key: None,
    ))
    script = (
        "Our advice is useful today. Scan the QR code and get the ebook "
        "to revisit these warning signs."
    )
    with pytest.raises(ValueError, match="PREFLIGHT_EBOOK_COVER_REQUIRED"):
        asyncio.run(run_professional_director(
            script, output=tmp_path, avatar=b"PNG", ebook=None, model="offline",
        ))
    assert calls == []
    assert not (tmp_path / "agent_openai" / "usage.json").exists()


def test_b11_and_valid_beats_survive_later_model_failure(tmp_path, monkeypatch):
    script, groups, core, cuts, _choices = _fixture()

    class FailingRunner:
        @staticmethod
        async def run(agent, payload, *, max_turns=1, hooks=None):
            if agent.output_type is GroupProposal:
                response = GroupProposal.model_validate({
                    "narrative_core": core, "blocks": groups,
                })
            elif agent.output_type is WholeScriptCuts:
                response = WholeScriptCuts.model_validate({
                    "blocks": [{"block_id": 1, "cuts": cuts}],
                })
            else:
                raise RuntimeError("model offline")
            usage = SimpleNamespace(
                requests=1, input_tokens=10, output_tokens=10,
                total_tokens=20, input_tokens_details=SimpleNamespace(cached_tokens=0),
            )
            if hooks:
                await hooks.on_llm_end(
                    None, agent, SimpleNamespace(usage=usage, response_id="fake"),
                )
            return SimpleNamespace(final_output=response,
                                   context_wrapper=SimpleNamespace(usage=usage))

    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        Agent=FakeAgent, Runner=FailingRunner,
        ModelBehaviorError=FakeModelBehaviorError,
        RunHooks=type("RunHooks", (), {}),
        set_default_openai_key=lambda key: None,
    ))
    with pytest.raises(RuntimeError, match="02_visuals"):
        asyncio.run(run_professional_director(
            script, output=tmp_path, avatar=b"PNG", ebook=None, model="offline",
        ))
    assert (tmp_path / "B1.1" / "output.json").is_file()
    assert (tmp_path / "B1.2" / "block_1" / "output.json").is_file()
    assert (tmp_path / "agent_openai" / "phases"
            / "02_planning" / "beats" / "output.json").is_file()
    assert not (tmp_path / "B2" / "merged_output.json").exists()


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
