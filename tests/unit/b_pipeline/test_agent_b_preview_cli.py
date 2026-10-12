"""Standalone Agent B CLI checks: real B1.1 fixture and no-network SDK preflight."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.pipeline.run_agent_b_preview import _parse_input, preflight

FIXTURE = Path(__file__).resolve().parents[3] / "examples" / "agent_b_preview_phone.json"


def test_fixture_contains_exact_chase_suspense_in_b11_order():
    blocks, core = _parse_input(FIXTURE)
    text = "".join(block.text for block in blocks)
    assert [block.block_id for block in blocks] == [1, 2, 3]
    assert text.startswith("Your phone rings. You look at the screen.")
    assert "And the number is the same one printed on the back" in text
    assert text.endswith("It matches.")
    assert core.central_question and core.final_answer


def test_preflight_uses_real_tools_without_runner_or_api_calls(tmp_path, monkeypatch):
    seen = {"agent": 0, "session": 0}

    def make_agent(**kwargs):
        seen["agent"] += 1
        assert len(kwargs["tools"]) == 11
        return SimpleNamespace(**kwargs)

    def make_session(session_id, db_path):
        seen["session"] += 1
        assert db_path.endswith("session.sqlite3")
        return SimpleNamespace(session_id=session_id)

    sdk = SimpleNamespace(
        Agent=make_agent,
        SQLiteSession=make_session,
        function_tool=lambda function: SimpleNamespace(name=function.__name__),
    )
    monkeypatch.setitem(sys.modules, "agents", sdk)
    blocks, core = _parse_input(FIXTURE)
    report = preflight(tmp_path, blocks, core, "test-model")
    assert report["status"] == "preflight_ok"
    assert report["api_calls"] == 0
    assert report["tool_count"] == 11
    assert report["project_status"] == "planning"
    assert seen == {"agent": 1, "session": 1}
    assert not (tmp_path / "visual_plan.json").exists()


def test_duplicate_b11_block_ids_fail_before_api(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["blocks"][1]["block_id"] = 1
    candidate = tmp_path / "bad.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unique ascending"):
        _parse_input(candidate)
