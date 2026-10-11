"""Offline end-to-end SDK orchestration tests: no paid requests."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from ai_video_factory.agents.professional.feedback import (
    approve_feedback,
    parse_feedback_arg,
)
from ai_video_factory.agents.professional.runner import run_professional_director
from ai_video_factory.bots.contracts import B11Output


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


def test_real_five_phase_controller_uses_one_agent_session(tmp_path, monkeypatch):
    script, blocks, core, beats = fixture_source()
    invocations = []

    class FakeRunner:
        @staticmethod
        async def run(agent, payload, *, session, max_turns=18):
            invocations.append((agent, session, payload))
            if agent.output_type is B11Output:
                final = B11Output.model_validate({
                    "pipeline_stage": "B1.1", "narrative_core": core,
                    "blocks": blocks, "error": None,
                })
            elif agent.output_type is not str and isinstance(agent.output_type, type):
                from ai_video_factory.agents.professional.runner import PlannedVideo

                assert agent.output_type is PlannedVideo
                final = PlannedVideo.model_validate({
                    "visual_style": "profesional", "narrative_core": core,
                    "blocks": [{"block_id": 1, "beats": beats}],
                })
            else:
                final = "OK"
            usage = SimpleNamespace(
                requests=1, input_tokens=100, output_tokens=50,
                total_tokens=150, input_tokens_details=SimpleNamespace(cached_tokens=5),
            )
            return SimpleNamespace(final_output=final, context_wrapper=SimpleNamespace(usage=usage))

    fake_agents = SimpleNamespace(
        Agent=FakeAgent, Runner=FakeRunner, SQLiteSession=FakeSession,
        function_tool=lambda function: function,
        set_default_openai_key=lambda key: None,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    result = asyncio.run(run_professional_director(
        script, output=tmp_path, avatar=b"PNG", ebook=None, model="test-model",
    ))
    assert len(invocations) == 5  # all five global stages, including creative correction
    assert len({id(item[1]) for item in invocations}) == 1
    assert all("Professional Director" in item[0].name for item in invocations)
    assert all(item[0].model == "test-model" for item in invocations)
    assert len(result["usage"]) == 5
    assert result["validation"]["status"] == "passed"
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
