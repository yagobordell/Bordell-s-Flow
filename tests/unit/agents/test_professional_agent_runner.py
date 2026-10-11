"""Offline end-to-end SDK orchestration tests: no paid requests."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from ai_video_factory.agents.professional.feedback import (
    approve_feedback,
    parse_feedback_arg,
)
from ai_video_factory.agents.professional.runner import (
    GroupProposal,
    run_professional_director,
)


class FakeAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def clone(self, **kwargs):
        return FakeAgent(**{**vars(self), **kwargs})


class FakeSession:
    def __init__(self, session_id, db_path):
        self.db_path = db_path


def fixture_source():
    parts = ["Una idea concreta. ", "Otra idea clara. ", "Una tercera prueba. ", "Y cerramos."]
    script = "".join(parts)
    blocks = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "neutro", "emotional_exit": "conclusion",
        "span": {"first_words": "Una idea concreta.", "last_words": "Y cerramos."},
    }]
    core = {"central_question": "¿Qué?", "final_answer": "Una conclusión."}
    beats = [
        {
            "beat_id": f"1{chr(65 + i)}", "text": part, "beat_type": "claim",
            "visual_type": "media_image", "prompt": None, "video_prompt": None,
            "audio": False, "reference": False,
            "image_prompts": [f"Distinct image {i + 1}"],
            "generation_mode": "independent", "editor_notes": [],
            "cta_group_id": None, "cta_reference_sources": None,
        }
        for i, part in enumerate(parts)
    ]
    beats[-1].update({
        "visual_type": "avatar", "image_prompts": None,
        "generation_mode": None, "audio": True,
    })
    return script, blocks, core, beats


@pytest.mark.parametrize("fail_first_json", [False, True])
@pytest.mark.parametrize("invented_ebook_cta", [False, True])
def test_real_five_phase_controller_uses_one_agent_session(
    tmp_path, monkeypatch, fail_first_json, invented_ebook_cta,
):
    script, blocks, core, beats = fixture_source()
    invocations = []
    planning_count = 0
    FakeModelBehaviorError = type("ModelBehaviorError", (Exception,), {})

    class FakeRunner:
        @staticmethod
        async def run(agent, payload, *, session, max_turns=18, hooks=None):
            nonlocal planning_count
            invocations.append((agent, session, payload))
            if agent.output_type is GroupProposal:
                assert max_turns == 1
            if agent.output_type is PlannedVideo:
                planning_count += 1
            if agent.output_type is GroupProposal:
                final = GroupProposal.model_validate({
                    "narrative_core": core, "blocks": blocks,
                })
            elif agent.output_type is not str and isinstance(agent.output_type, type):
                from ai_video_factory.agents.professional.runner import PlannedVideo

                assert agent.output_type is PlannedVideo
                proposed_beats = copy.deepcopy(beats)
                if invented_ebook_cta and planning_count == 1:
                    proposed_beats[0]["cta_group_id"] = 1
                    proposed_beats[0]["cta_reference_sources"] = ["ebook"]
                    proposed_beats[0]["reference"] = True
                final = PlannedVideo.model_validate({
                    "visual_style": "profesional", "narrative_core": core,
                    "blocks": [{"block_id": 1, "beats": proposed_beats}],
                })
            else:
                final = "OK"
            usage = SimpleNamespace(
                requests=1, input_tokens=100, output_tokens=50,
                total_tokens=150, input_tokens_details=SimpleNamespace(cached_tokens=5),
            )
            if hooks is not None:
                await hooks.on_llm_end(
                    SimpleNamespace(usage=usage), agent,
                    SimpleNamespace(usage=usage, response_id="test-response",
                                    request_id="test-request"),
                )
            if fail_first_json and len(invocations) == 1:
                raise FakeModelBehaviorError("Invalid JSON when parsing model output")
            return SimpleNamespace(final_output=final, context_wrapper=SimpleNamespace(usage=usage))

    fake_agents = SimpleNamespace(
        Agent=FakeAgent, Runner=FakeRunner, SQLiteSession=FakeSession,
        function_tool=lambda function: function,
        set_default_openai_key=lambda key: None,
        ModelBehaviorError=FakeModelBehaviorError,
        ModelSettings=lambda **kwargs: SimpleNamespace(**kwargs),
        RunHooks=type("RunHooks", (), {}),
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    result = asyncio.run(run_professional_director(
        script, output=tmp_path, avatar=b"PNG", ebook=None, model="test-model",
    ))
    expected_calls = 6 if fail_first_json else 5
    assert len(invocations) == expected_calls
    assert len({id(item[1]) for item in invocations}) == 1
    assert all("Professional Director" in item[0].name for item in invocations)
    assert all(item[0].model == "test-model" for item in invocations)
    assert invocations[0][0].tools == []
    assert invocations[0][0].model_settings.max_tokens == 8192
    assert invocations[2 if fail_first_json else 1][0].tools == []
    assert all(
        tool.__name__ != "submit_plan_revision"
        for tool in invocations[4 if fail_first_json else 3][0].tools
    )
    assert len(result["usage"]) == expected_calls
    if fail_first_json:
        assert result["usage"][0]["status"] == "invalid_structured_output"
        assert result["usage"][0]["total_tokens"] == 150
        assert (tmp_path / "agent_openai" / "01_groups_invalid_json_1.json").is_file()
    assert result["validation"]["status"] == "passed"
    if invented_ebook_cta:
        initial = json.loads(
            (tmp_path / "agent_openai" / "plan_initial.json").read_text()
        )
        assert any(
            issue["rule_id"] == "CTA_COVER_UNAVAILABLE"
            for issue in initial["validation"]["issues"]
        )
        assert not any(
            beat.get("cta_group_id")
            for block in result["projection"]["visual_plan"]["blocks"]
            for beat in block["beats"]
        )
    phase2_request = invocations[2 if fail_first_json else 1][2]
    assert "cover is NOT provided" in phase2_request[0]["content"][0]["text"]
    saved = json.loads((tmp_path / "agent_openai" / "approved_plan.json").read_text())
    assert saved["source_sha256"] == result["validation"]["source_sha256"]
    assert result["projection"]["visual_plan"]["visual_style"] == "profesional"


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
