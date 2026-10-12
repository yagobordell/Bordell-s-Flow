"""No-network tests for actual callable tools, pinned guidance and autonomous run acceptance."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from ai_video_factory.bots.agent_b.instructions import InstructionBundle
from ai_video_factory.bots.agent_b.project import AgentBProject
from ai_video_factory.bots.agent_b.runner import AgentBRunError, run_agent_b
from ai_video_factory.bots.agent_b.tools import ProductionToolService
from ai_video_factory.bots.contracts import MaterializedBlock

CORE = {"central_question": "What happened?", "final_answer": "An explanation."}


def source(text="It rings."):
    return MaterializedBlock(block_id=1, type="intro", emotional_entry="tense",
                             emotional_exit="curious", text=text)


def plan(src):
    return {"schema_version": "b-pipeline-v1", "visual_style": "profesional",
            "narrative_core": CORE,
            "blocks": [{"block_id": 1, "type": "intro", "emotional_entry": "tense",
                        "emotional_exit": "curious", "beats": [
                            {"beat_id": "1A", "text": src.text, "beat_type": "action",
                             "visual_type": "media_image", "prompt": "A ringing telephone.",
                             "video_prompt": None, "audio": False, "reference": False}
                        ]}]}


def test_pinned_instruction_bundle_rejects_bad_ranges():
    bundle = InstructionBundle.load()
    assert len(bundle.sha256) == 64
    assert "single autonomous audiovisual director" in bundle.starting_instructions()
    assert bundle.get("visual_direction.md", limit=50)["text"]
    with pytest.raises(ValueError):
        bundle.get("ARCHITECTURE.md")
    with pytest.raises(ValueError):
        bundle.get("core.md", offset=-1)


def test_tool_service_performs_real_validation_and_save(tmp_path):
    src = source()
    bundle = InstructionBundle.load()
    project = AgentBProject(tmp_path, [src], CORE, instruction_sha256=bundle.sha256)
    tools = ProductionToolService(project, bundle, max_calls=15)
    assert "It rings." in tools.read_script()
    assert json.loads(tools.read_group([1]))["blocks"][0]["block_id"] == 1
    assert json.loads(tools.read_instructions("core.md", limit=200))["status"] == "ok"
    broken = plan(src)
    broken["blocks"][0]["beats"][0]["text"] = "Changed."
    assert json.loads(tools.validate_plan(json.dumps(broken)))["status"] == "invalid"
    assert json.loads(tools.read_validation_report())["errors"]
    assert json.loads(tools.save_visual_plan(json.dumps(broken), 0))["status"] == "invalid"
    good = plan(src)
    assert json.loads(tools.save_visual_plan(json.dumps(good), 0))["revision"] == 1
    assert project.status()["revision"] == 1
    assert json.loads(tools.save_visual_plan(json.dumps(good), 0))["status"] == "stale"


def test_tool_budget_and_instruction_isolation(tmp_path):
    bundle = InstructionBundle.load()
    p = AgentBProject(tmp_path, [source()], CORE, instruction_sha256=bundle.sha256)
    tools = ProductionToolService(p, bundle, max_calls=1)
    tools.read_project_status()
    with pytest.raises(RuntimeError, match="budget"):
        tools.read_script()


def test_autonomous_sdk_runner_saves_and_freezes_without_api(tmp_path, monkeypatch):
    src = source()
    accepted = plan(src)
    calls = []

    class FakeRunner:
        @staticmethod
        async def run(agent, prompt, session, max_turns):
            calls.append((prompt, session.session_id, max_turns))
            all_tools = {tool.__name__: tool for tool in agent.tools}
            source_text = json.loads(all_tools["read_script"]())["text"]
            assert source_text == src.text
            assert json.loads(all_tools["validate_plan"](json.dumps(accepted)))["valid"]
            assert json.loads(all_tools["save_visual_plan"](json.dumps(accepted), 0))[
                "status"] == "saved"
            return SimpleNamespace(final_output="Planned")

    class FakeAgent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]

    class FakeSession:
        def __init__(self, session_id, db_path):
            self.session_id = session_id
            self.db_path = db_path

    fake = SimpleNamespace(Agent=FakeAgent, Runner=FakeRunner, SQLiteSession=FakeSession,
                           function_tool=lambda function: function)
    monkeypatch.setitem(sys.modules, "agents", fake)
    actual = asyncio.run(run_agent_b(tmp_path, [src], CORE, model="test-model", sdk=fake))
    assert actual == accepted
    assert len(calls) == 1
    assert asyncio.run(run_agent_b(tmp_path, [src], CORE,
                                   model="test-model", sdk=fake)) == accepted
    assert len(calls) == 1


def test_bounded_failure_never_freezes(tmp_path, monkeypatch):
    class FakeRunner:
        @staticmethod
        async def run(*args, **kwargs):
            return SimpleNamespace(final_output="No plan")

    fake = SimpleNamespace(Agent=lambda **kw: None, Runner=FakeRunner,
                           SQLiteSession=lambda *args: None,
                           function_tool=lambda function: function)
    monkeypatch.setitem(sys.modules, "agents", fake)
    with pytest.raises(AgentBRunError, match="No valid unified"):
        asyncio.run(run_agent_b(tmp_path, [source()], CORE, model="test-model",
                                max_attempts=2, sdk=fake))
    bundle = InstructionBundle.load()
    project = AgentBProject(tmp_path, [source()], CORE, instruction_sha256=bundle.sha256)
    assert project.status()["status"] == "planning"


def test_partial_block_drafts_assemble_a_single_global_plan(tmp_path):
    first = source("It rings.")
    second = MaterializedBlock(block_id=2, type="development", emotional_entry="waiting",
                               emotional_exit="settled", text="It matches.")
    bundle = InstructionBundle.load()
    p = AgentBProject(tmp_path, [first, second], CORE, instruction_sha256=bundle.sha256)
    first_block = plan(first)["blocks"][0]
    first_block["beats"][0]["prompt"] = "A1. Same phone ringing."
    assert p.save_block_draft(1, first_block, expected_revision=0) == 1
    with pytest.raises(Exception, match="Plan rejected"):
        p.finalize_drafts(expected_revision=1)
    second_block = {"block_id": 2, "type": "development", "emotional_entry": "waiting",
                    "emotional_exit": "settled", "beats": [{
                        "beat_id": "2A", "text": second.text, "beat_type": "action",
                        "visual_type": "media_image", "prompt": "A2. Same phone and person.",
                        "video_prompt": None, "audio": False, "reference": False}]}
    assert p.save_block_draft(2, second_block, expected_revision=1) == 2
    assert p.status()["drafted_block_ids"] == [1, 2]
    assert [x["label"] for x in p.status()["sequence_index"]["A"]] == ["A1", "A2"]
    assert p.finalize_drafts(expected_revision=2) == 3
    p.freeze(expected_revision=3)
    assert [b["block_id"] for b in p.frozen_plan()["blocks"]] == [1, 2]
