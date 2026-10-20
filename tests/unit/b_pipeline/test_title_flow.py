"""Offline contract and execution tests for the Title flow; no paid providers."""
from __future__ import annotations

import copy
import json

import pytest

from ai_video_factory.bots import title_flow
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.openai_batch_images import described_beats


def _plan() -> dict:
    return {
        "schema_version": "b-planner-v1",
        "narrative_contract": {
            "central_question": "How?", "final_answer": "This way.",
        },
        "word_budget_total": 4,
        "stages": [
            {"id": "S01", "kind": "hook", "markers": [], "word_budget": 2},
            {"id": "S02", "kind": "chain", "markers": [], "word_budget": 1},
            {"id": "S03", "kind": "closing", "markers": [], "word_budget": 1},
        ],
    }


def _block(n: int, *, bad: bool = False) -> dict:
    kind = ("intro", "development", "close")[n - 1]
    content = ("First scene.", "Next.", "Goodbye.")[n - 1]
    p = ("A1. Woman views phone at a desk.", "A2. Woman picks up phone at a desk.", None)[n - 1]
    return {"block": {
        "block_id": n, "type": kind, "emotional_entry": "Expecting an answer",
        "emotional_exit": "More informed",
        "span": {"first_words": content, "last_words": content},
        "beats": [{
            "beat_id": f"{n}A", "text": content * 120 if bad else content,
            "beat_type": "action",
            "visual_type": "avatar" if n == 3 else "media_image",
            "prompt": p, "video_prompt": None,
            "reference": False, "audio": n == 3,
        }],
    }}


def _response(payload: dict) -> dict:
    return {
        "stop_reason": "end_turn", "model": title_flow.MODEL,
        "content": [
            {"type": "thinking", "thinking": "signed reasoning", "signature": "sig"},
            {"type": "text", "text": json.dumps(payload)},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 10},
    }


def test_plan_rejects_wrong_budget():
    plan = _plan()
    title_flow.validate_plan(plan)
    plan["word_budget_total"] = 5
    with pytest.raises(title_flow.TitleContractError, match="budgets"):
        title_flow.validate_plan(plan)


def test_rejects_beat_length_and_avatar_overrun():
    plan = _plan()
    with pytest.raises(title_flow.TitleContractError, match="too long"):
        title_flow.validate_block(_block(1, bad=True), 1, plan, [])
    first = _block(1)["block"]
    second = _block(2)["block"]
    second["beats"][0]["audio"] = True
    second["beats"][0]["visual_type"] = "avatar"
    second["beats"][0]["prompt"] = None
    title_flow.validate_block({"block": first}, 1, plan, [])


def test_resumable_run_replays_signed_content_without_new_t1_call(tmp_path):
    calls = []

    def fake_provider(*, api_key, system, messages):
        assert api_key == "fake"
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            return _response(_plan())
        # The second and later T2 turns include every original accepted
        # assistant block, not just the parsed JSON text.
        assistant_contents = [
            msg["content"] for msg in messages if msg["role"] == "assistant"
        ]
        for content in assistant_contents:
            assert content[0]["type"] == "thinking"
            assert content[0]["signature"] == "sig"
        return _response(_block(len(assistant_contents) + 1))

    kwargs = {
        "output": tmp_path, "avatar": "Uncle Enzo",
        "title": "Why this happened", "ebook": b"cover",
        "thumbnail": b"thumb", "api_key": "fake",
        "provider": fake_provider,
    }
    first, none = title_flow.run(**kwargs, stop="T1")
    assert none is None
    assert first == _plan()
    _, value = title_flow.run(**kwargs, stop="T2")
    assert value["schema_version"] == "b-pipeline-merged-v1"
    assert len(calls) == 4
    title_flow.run(**kwargs, stop="T2")
    assert len(calls) == 4
    assert (tmp_path / "T2" / "blocks" / "block_001" / "response.json").is_file()


def test_globally_lettered_groups_become_cross_block_image_references():
    p = _plan()
    blocks = [_block(i)["block"] for i in range(1, 4)]
    m = title_flow.merged(p, blocks)
    visual = title_flow.image_plan(m)
    assert visual["schema_version"] == "b-pipeline-v1"
    assert visual["blocks"][1]["beats"][0]["title_global_group"] == "A"
    assert visual["blocks"][1]["beats"][0]["prompt"].startswith("Next")
    jobs = plan_image_jobs(visual, described_beats(visual))
    first, second = jobs
    assert second["continuity_previous_asset_id"] == first["asset_id"]
    assert second["continuity_previous_block_id"] == first["block_id"]


def test_failed_claude_turn_does_not_trigger_blind_replay(tmp_path):
    def fail(*, api_key, system, messages):
        raise title_flow.ClaudeAPIError("uncertain")

    kwargs = {
        "output": tmp_path, "avatar": "Enzo", "title": "Title",
        "ebook": b"a", "thumbnail": b"b", "api_key": "fake",
    }
    with pytest.raises(title_flow.ClaudeAPIError):
        title_flow.run(**kwargs, stop="T1", provider=fail)
    with pytest.raises(title_flow.TitleContractError, match="uncertain"):
        title_flow.run(**kwargs, stop="T1", provider=fail)


def test_title_inputs_refuse_mutation(tmp_path):
    def plan_only(*, api_key, system, messages):
        return _response(_plan())

    title_flow.run(
        output=tmp_path, avatar="Enzo", title="Original",
        ebook=b"cover", thumbnail=b"thumb", api_key="key", stop="T1",
        provider=plan_only,
    )
    with pytest.raises(title_flow.TitleContractError, match="changed"):
        title_flow.run(
            output=tmp_path, avatar="Enzo", title="New",
            ebook=b"cover", thumbnail=b"thumb", api_key="key", stop="T1",
            provider=plan_only,
        )
