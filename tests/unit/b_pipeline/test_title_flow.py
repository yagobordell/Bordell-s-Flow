"""Offline contract and execution tests for the Title flow; no paid providers."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from urllib.error import HTTPError

import pytest

from ai_video_factory.bots import title_flow
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.openai_batch_images import described_beats


def _plan() -> dict:
    return {
        "schema_version": "b-planner-v1",
        "narrative_contract": {
            "central_question": "How?", "final_answer": "This way.",
            "narrator": {"background": "A protective guide", "manner": "Clear and warm"},
        },
        "word_budget_total": 4,
        "macro_revelation": {"stage_id": "S02", "statement": "The actual answer"},
        "continuity_base": {
            "persistent_worlds": [], "recurring_people": [],
            "places": [], "devices_and_interfaces": [],
        },
        "stages": [
            {"id": "S01", "kind": "hook", "markers": ["hook"],
             "word_budget": 2, "facts": ["First scene."]},
            {"id": "S02", "kind": "chain", "markers": ["macro_revelation"],
             "word_budget": 1, "facts": ["A different fact."]},
            {"id": "S03", "kind": "closing", "markers": ["closing_return"],
             "word_budget": 1, "facts": ["Last scene."]},
        ],
    }


def _block(n: int, *, bad: bool = False) -> dict:
    kind = ("intro", "development", "close")[n - 1]
    content = ("First scene. ", "Next. ", "Goodbye.")[n - 1]
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
    assert visual["blocks"][1]["beats"][0]["prompt"].startswith("Woman picks up")
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


@pytest.mark.parametrize("status", [400, 401, 403, 413, 429])
def test_http_rejection_shows_status_without_ambiguous_result(status):
    def http_failure(request, timeout):
        body = json.dumps({
            "error": {
                "type": "invalid_request_error",
                "message": "Invalid Anthropic request",
            }
        }).encode("utf-8")
        raise HTTPError(
            request.full_url, status, "Rejected", {},
            io.BytesIO(body),
        )

    with pytest.raises(title_flow.ClaudeRequestRejected, match=f"HTTP {status}"):
        title_flow.call_claude(
            api_key="invalid", system="test", messages=[
                {"role": "user", "content": "test"},
            ], transport=http_failure,
        )


def test_server_error_still_retains_ambiguous_status():
    def http_failure(request, timeout):
        raise HTTPError(
            request.full_url, 503, "Unavailable", {},
            io.BytesIO(b""),
        )

    with pytest.raises(title_flow.ClaudeAPIError, match="uncertain"):
        title_flow.call_claude(
            api_key="invalid", system="test", messages=[
                {"role": "user", "content": "test"},
            ], transport=http_failure,
        )


def test_definitive_t1_rejection_can_retry_after_fix(tmp_path):
    called = []

    def rejected(*, api_key, system, messages):
        called.append("rejected")
        raise title_flow.ClaudeRequestRejected("HTTP 401")

    kwargs = {
        "output": tmp_path, "avatar": "Uncle_Enzo", "title": "Wrong Number",
        "ebook": b"cover", "thumbnail": b"thumb", "api_key": "fake",
        "stop": "T1",
    }
    with pytest.raises(title_flow.ClaudeRequestRejected, match="401"):
        title_flow.run(**kwargs, provider=rejected)
    assert not (tmp_path / "T1" / "inflight_attempt_1.json").exists()
    assert (tmp_path / "T1" / "input.json").is_file()

    def working(*, api_key, system, messages):
        called.append("accepted")
        return _response(_plan())

    plan, final = title_flow.run(**kwargs, provider=working)
    assert final is None
    assert plan == _plan()
    assert called == ["rejected", "accepted"]


def test_definitive_t2_rejection_retains_accepted_t1(tmp_path):
    kwargs = {
        "output": tmp_path, "avatar": "Uncle_Enzo", "title": "Wrong Number",
        "ebook": b"cover", "thumbnail": b"thumb", "api_key": "fake",
    }

    def preflight(*, api_key, system, messages):
        return _response(_plan())

    title_flow.run(**kwargs, stop="T1", provider=preflight)

    def rejected(*, api_key, system, messages):
        raise title_flow.ClaudeRequestRejected("HTTP 429")

    with pytest.raises(title_flow.ClaudeRequestRejected, match="429"):
        title_flow.run(**kwargs, stop="T2", provider=rejected)
    t2_root = tmp_path / "T2" / "blocks" / "block_001"
    assert not (t2_root / "inflight_attempt_1.json").exists()
    assert (tmp_path / "T1" / "output.json").is_file()



def test_t2_legacy_max_tokens_exhaustion_recovers_on_fourth_only(tmp_path):
    """Replay all three billable capped turns; send only one new request."""
    old_calls = []

    def legacy_provider(*, api_key, system, messages):
        old_calls.append(len(messages))
        return {
            "model": title_flow.MODEL,
            "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "too long", "signature": "sig"}],
            "usage": {"input_tokens": 500, "output_tokens": 32000},
        }

    options = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"ebook",
        "thumbnail": b"thumb", "api_key": "fake",
    }
    title_flow.run(**options, stop="T1", provider=lambda **kw: _response(_plan()))

    with pytest.raises(title_flow.TitleContractError, match="3 attempts exhausted"):
        title_flow.run(**options, stop="T2", provider=legacy_provider)
    assert len(old_calls) == 3

    root = tmp_path / "T2" / "blocks" / "block_001"
    for attempt in (1, 2, 3):
        assert title_flow.read(root / f"response_attempt_{attempt}.json")[
            "stop_reason"
        ] == "max_tokens"
        assert (root / f"rejection_{attempt}.json").is_file()
        assert not (root / f"inflight_attempt_{attempt}.json").exists()

    new_calls = []

    def recovered_provider(*, api_key, system, messages):
        count = sum(msg["role"] == "assistant" for msg in messages)
        new_calls.append(count)
        # Invalid T2 turns are never inserted into accepted conversation history.
        return _response(_block(count + 1))

    _, value = title_flow.run(**options, stop="T2", provider=recovered_provider)
    assert [block["block_id"] for block in value["blocks"]] == [1, 2, 3]
    assert new_calls == [0, 1, 2]
    assert (root / "response_attempt_4.json").is_file()
    assert (root / "response.json").is_file()
    assert title_flow.read(root / "response_attempt_1.json")[
        "stop_reason"
    ] == "max_tokens"
    # A third resume must use accepted checkpoints, not replay any paid requests.
    title_flow.run(
        **options, stop="T2",
        provider=lambda **kwargs: pytest.fail("An accepted T2 turn was re-sent"),
    )


def test_new_max_tokens_exhaustion_does_not_enable_unbounded_fourth_call(tmp_path):
    options = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"ebook",
        "thumbnail": b"thumb", "api_key": "fake",
    }
    title_flow.run(**options, stop="T1", provider=lambda **kw: _response(_plan()))
    calls = []

    def exhausted(*, api_key, system, messages):
        calls.append(messages)
        return {
            "model": title_flow.MODEL, "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "unfinished", "signature": "sig"}],
            "usage": {
                "input_tokens": 120,
                "output_tokens": title_flow.T2_MAX_OUTPUT_TOKENS,
            },
        }

    with pytest.raises(title_flow.TitleContractError, match="3 attempts exhausted"):
        title_flow.run(**options, stop="T2", provider=exhausted)
    assert len(calls) == 3
    with pytest.raises(title_flow.TitleContractError, match="3 attempts exhausted"):
        title_flow.run(**options, stop="T2", provider=lambda **kw: pytest.fail(
            "Exhausted 128k block must not be silently regenerated"
        ))
    assert not (tmp_path / "T2" / "blocks" / "block_001" /
                "response_attempt_4.json").exists()


def test_t2_provider_supports_128k_ceiling_without_lowering_high_effort():
    captured = []

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "{}"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ]
            return iter([
                ("data: " + json.dumps(event)).encode()
                for event in events
            ])

    def transport(request, timeout):
        captured.append(json.loads(request.data))
        return FakeStream()

    response = title_flow.call_claude(
        api_key="fake",
        system="T2",
        messages=[{"role": "user", "content": "Write block 1."}],
        max_tokens=title_flow.T2_MAX_OUTPUT_TOKENS,
        transport=transport,
    )
    assert response["stop_reason"] == "end_turn"
    assert captured[0]["max_tokens"] == 128000
    assert captured[0]["output_config"]["effort"] == "high"
    assert captured[0]["stream"] is True
    assert "temperature" not in captured[0]



def test_retry_feedback_is_preserved_before_signed_thinking_in_following_turn(tmp_path):
    """Catch Anthropic's 400: retry feedback was omitted after accepted block."""
    t2_requests = []
    failed_once = set()
    kwargs = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"cover",
        "thumbnail": b"thumbnail", "api_key": "fake",
    }

    def signed_provider(*, api_key, system, messages):
        assert api_key == "fake"
        if system == title_flow.prompt("t1_planner.md"):
            return _response(_plan())
        t2_requests.append(copy.deepcopy(messages))
        # Mimic Claude's binding of each signed thinking block to its
        # *entire prior conversation*, not merely its own text/signature.
        for index, message in enumerate(messages):
            if message["role"] != "assistant":
                continue
            content = message["content"]
            signed = content[0]
            assert signed["type"] == "thinking"
            prefix = json.dumps(messages[:index], ensure_ascii=False, sort_keys=True)
            expected = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
            assert signed["signature"] == "bound:" + expected

        number = sum(m["role"] == "assistant" for m in messages) + 1
        if number == 1 and number not in failed_once:
            failed_once.add(number)
            return _response({"invalid_block": True})
        result = _response(_block(number))
        prefix = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        result["content"][0]["signature"] = (
            "bound:" + hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        )
        return result

    title_flow.run(**kwargs, stop="T1", provider=signed_provider)
    _, merged = title_flow.run(**kwargs, stop="T2", provider=signed_provider)
    assert len(merged["blocks"]) == 3
    assert len(t2_requests) == 4
    retry_message = t2_requests[1][-1]
    assert retry_message["role"] == "user"
    assert "previous block 1 failed local validation" in retry_message["content"]
    assert t2_requests[2][:len(t2_requests[1])] == t2_requests[1]
    assert t2_requests[2][len(t2_requests[1])]["role"] == "assistant"
    assert (tmp_path / "T2" / "blocks" / "block_001" /
            "response_attempt_2.json").exists()


def test_resume_after_accepted_retry_reconstructs_exact_signed_prefix(tmp_path):
    """Accept block 1 on retry; simulate 400 at block 2; resume unchanged."""
    kwargs = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"cover",
        "thumbnail": b"thumbnail", "api_key": "fake",
    }
    title_flow.run(
        **kwargs, stop="T1", provider=lambda **kw: _response(_plan()),
    )

    calls = []

    def before_restart(*, api_key, system, messages):
        number = sum(msg["role"] == "assistant" for msg in messages) + 1
        calls.append(number)
        if number == 1 and calls.count(1) == 1:
            return _response({"bad_block": True})
        if number == 2:
            raise title_flow.ClaudeRequestRejected("HTTP 400 temporary test")
        output = _response(_block(1))
        prefix = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        output["content"][0]["signature"] = (
            "bound:" + hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        )
        return output

    with pytest.raises(title_flow.ClaudeRequestRejected, match="HTTP 400"):
        title_flow.run(**kwargs, stop="T2", provider=before_restart)

    first_root = tmp_path / "T2" / "blocks" / "block_001"
    raw_before = (first_root / "response.json").read_bytes()
    assert (first_root / "output.json").is_file()
    assert calls == [1, 1, 2]
    assert not (tmp_path / "T2" / "blocks" / "block_002" /
                "inflight_attempt_1.json").exists()

    resumed_calls = []

    def after_restart(*, api_key, system, messages):
        number = sum(msg["role"] == "assistant" for msg in messages) + 1
        resumed_calls.append(number)
        assistant_index = next(
            i for i, msg in enumerate(messages) if msg["role"] == "assistant"
        )
        signed = messages[assistant_index]["content"][0]
        prefix = json.dumps(
            messages[:assistant_index], ensure_ascii=False, sort_keys=True
        )
        assert signed["signature"] == (
            "bound:" + hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        )
        assert "previous block 1 failed local validation" in (
            messages[assistant_index - 1]["content"]
        )
        return _response(_block(number))

    _, value = title_flow.run(**kwargs, stop="T2", provider=after_restart)
    assert len(value["blocks"]) == 3
    assert resumed_calls == [2, 3]
    assert (first_root / "response.json").read_bytes() == raw_before


def test_v32_rejects_missing_narrator_or_insufficient_facts():
    plan = _plan()
    del plan["narrative_contract"]["narrator"]
    with pytest.raises(title_flow.TitleContractError, match="narrator"):
        title_flow.validate_plan(plan)
    plan = _plan()
    plan["stages"][1]["word_budget"] = 45
    plan["word_budget_total"] = 48
    with pytest.raises(title_flow.TitleContractError, match="facts"):
        title_flow.validate_plan(plan)


@pytest.mark.parametrize("broken", ["hook_marker", "close_marker", "macro_stage"])
def test_v32_rejects_broken_stage_markers(broken):
    plan = _plan()
    if broken == "hook_marker":
        plan["stages"][1]["markers"] = ["macro_revelation", "hook"]
    elif broken == "close_marker":
        plan["stages"][2]["markers"] = []
    else:
        plan["macro_revelation"]["stage_id"] = "S03"
    with pytest.raises(title_flow.TitleContractError, match="marker|macro"):
        title_flow.validate_plan(plan)


def test_v32_recurring_worlds_cover_three_stage_people():
    plan = _plan()
    plan["continuity_base"]["recurring_people"] = [{
        "name": "Viewer", "stages": ["S01", "S02", "S03"],
    }]
    with pytest.raises(title_flow.TitleContractError, match="persistent world"):
        title_flow.validate_plan(plan)
    plan["continuity_base"]["persistent_worlds"] = [{"name": "Viewer"}]
    title_flow.validate_plan(plan)


def test_v26_broll_resets_presenter_limit_between_blocks():
    plan = _plan()
    prior = _block(2)["block"]
    prior["beats"][0].update({
        "text": "Speaking safely. " * 5,
        "visual_type": "avatar", "prompt": None, "audio": True,
    })
    close = _block(3)
    base = close["block"]["beats"][0]
    first = dict(base)
    first.update({
        "beat_id": "3A", "text": "A visible change. ",
        "visual_type": "media_image", "prompt": "A note on a wooden desk",
        "audio": False,
    })
    last = dict(base)
    last.update({
        "beat_id": "3B", "text": ("One final line. " * 5).rstrip(),
        "visual_type": "avatar", "prompt": None, "audio": True,
    })
    close["block"]["beats"] = [first, last]
    title_flow.validate_block(close, 3, plan, [prior])


def test_v26_first_cta_requires_ebook_cover_reference():
    plan = _plan()
    plan["stages"][1]["kind"] = "aside"
    plan["stages"][1]["markers"] = ["macro_revelation", "cta_intermediate"]
    cta = _block(2)
    # CTA must be ungrouped: a grid label is independently invalid here.
    cta["block"]["beats"][0]["prompt"] = "Woman picks up phone at a desk."
    cta["block"]["beats"][0].update({
        "cta_group_id": 1,
        "cta_reference_sources": [],
    })
    with pytest.raises(title_flow.TitleContractError, match="ebook cover"):
        title_flow.validate_block(cta, 2, plan, [_block(1)["block"]])
    cta["block"]["beats"][0]["cta_reference_sources"] = ["ebook"]
    cta["block"]["beats"][0]["reference"] = True
    title_flow.validate_block(cta, 2, plan, [_block(1)["block"]])


def test_v26_grid_continuity_can_cross_resource_cta():
    """v26 groups are global and cannot be banned merely for crossing CTA."""
    plan = _plan()
    # Existing accepted blocks may contain a legitimate global sequence.
    prior = _block(1)["block"]
    cta = _block(2)["block"]
    cta["beats"][0]["prompt"] = "An unmarked CTA still."
    cta["beats"][0].update({
        "cta_group_id": 1, "cta_reference_sources": ["ebook"],
        "reference": True,
    })
    close = _block(3)
    still = dict(close["block"]["beats"][0])
    still.update({
        "beat_id": "3A", "text": "The same phone again. ",
        "visual_type": "media_image", "prompt": "A2. Woman picks up phone at a desk.",
        "reference": False, "audio": False,
    })
    anchor = dict(close["block"]["beats"][0])
    anchor["beat_id"] = "3B"
    close["block"]["beats"] = [still, anchor]
    title_flow.validate_block(close, 3, plan, [prior, cta])


def test_v32_t1_retry_explains_saved_validation_failure(tmp_path):
    """A strict v32 rejection must be actionable, not a repeated paid guess."""
    calls = []

    def provider(*, api_key, system, messages):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            invalid = _plan()
            invalid["narrative_contract"].pop("narrator")
            return _response(invalid)
        assert "narrator background/manner missing" in messages[-1]["content"]
        return _response(_plan())

    plan, result = title_flow.run(
        output=tmp_path, avatar="Uncle_Enzo",
        title="Wrong Number", ebook=b"cover", thumbnail=b"thumb",
        api_key="fake", stop="T1", provider=provider,
    )
    assert result is None
    assert plan == _plan()
    assert len(calls) == 2
    assert "narrator" in title_flow.read(
        tmp_path / "T1" / "rejection_attempt_1.json"
    )["reason"]
    assert (tmp_path / "T1" / "output.json").is_file()


def test_v32_t1_resume_reconstructs_legacy_feedback_without_paid_repeat(tmp_path):
    invalid = _plan()
    invalid["stages"][1]["facts"] = []
    root = tmp_path / "T1"
    options = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"cover",
        "thumbnail": b"thumb", "api_key": "fake", "stop": "T1",
    }
    calls = []

    def interrupted(*, api_key, system, messages):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            return _response(invalid)
        raise title_flow.ClaudeAPIError("connection broken")

    with pytest.raises(title_flow.ClaudeAPIError):
        title_flow.run(**options, provider=interrupted)
    assert (root / "response_attempt_1.json").is_file()
    (root / "rejection_attempt_1.json").unlink()
    # This was an uncertain request; never erase its inflight marker to
    # replay it. The first response and newly rebuilt rejection are retained.
    with pytest.raises(title_flow.TitleContractError, match="uncertain"):
        title_flow.run(**options, provider=lambda **kw: pytest.fail("Must not resend"))
    assert "only 0 facts" in title_flow.read(
        root / "rejection_attempt_1.json"
    )["reason"]



def test_t1_legacy_32k_max_tokens_resumes_with_only_one_new_128k_request(tmp_path):
    """The user's original three paid T1 turns remain byte-for-byte intact."""
    options = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"cover",
        "thumbnail": b"thumb", "api_key": "fake", "stop": "T1",
    }
    old_calls = []

    def old(*, api_key, system, messages):
        old_calls.append(copy.deepcopy(messages))
        return {
            "model": title_flow.MODEL, "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "capped", "signature": "sig"}],
            "usage": {"input_tokens": 500, "output_tokens": 32000},
        }

    with pytest.raises(title_flow.TitleContractError, match="3 attempts exhausted"):
        title_flow.run(**options, provider=old)
    assert len(old_calls) == 3
    root = tmp_path / "T1"
    original = [
        (root / f"response_attempt_{n}.json").read_bytes()
        for n in (1, 2, 3)
    ]

    calls = []

    def resumed(*, api_key, system, messages):
        calls.append(copy.deepcopy(messages))
        assert "previous T1 response failed" in messages[-1]["content"]
        assert "max_tokens" in messages[-1]["content"]
        return _response(_plan())

    accepted, result = title_flow.run(**options, provider=resumed)
    assert accepted == _plan()
    assert result is None
    assert len(calls) == 1
    assert (root / "response_attempt_4.json").is_file()
    assert all(
        (root / f"response_attempt_{n}.json").read_bytes() == original[n-1]
        for n in (1, 2, 3)
    )
    assert (root / "output.json").is_file()

    title_flow.run(
        **options, provider=lambda **kwargs: pytest.fail(
            "T1 must not be re-submitted after validation"
        ),
    )


def test_new_128k_t1_max_tokens_does_not_grant_fourth_request(tmp_path):
    options = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"cover",
        "thumbnail": b"thumb", "api_key": "fake", "stop": "T1",
    }
    calls = []

    def exhausted(*, api_key, system, messages):
        calls.append(1)
        return {
            "model": title_flow.MODEL, "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "large", "signature": "sig"}],
            "usage": {
                "input_tokens": 500,
                "output_tokens": title_flow.T1_MAX_OUTPUT_TOKENS,
            },
        }

    with pytest.raises(title_flow.TitleContractError, match="3 attempts exhausted"):
        title_flow.run(**options, provider=exhausted)
    assert len(calls) == 3
    with pytest.raises(title_flow.TitleContractError, match="3 attempts exhausted"):
        title_flow.run(
            **options,
            provider=lambda **kwargs: pytest.fail(
                "128k T1 attempts exhausted; paid request must not replay"
            ),
        )
    assert not (tmp_path / "T1" / "response_attempt_4.json").exists()


def test_t1_stream_supports_128k_high_effort():
    seen = []

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "{}"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 2}},
                {"type": "message_stop"},
            ]
            return iter(
                [("data: " + json.dumps(item)).encode() for item in events]
            )

    def transport(request, timeout):
        seen.append(json.loads(request.data))
        return FakeStream()

    reply = title_flow.call_claude(
        api_key="fake", system="T1", messages=[
            {"role": "user", "content": "Plan this story."},
        ], max_tokens=title_flow.T1_MAX_OUTPUT_TOKENS, transport=transport,
    )
    assert reply["stop_reason"] == "end_turn"
    assert seen[0]["max_tokens"] == 128000
    assert seen[0]["output_config"]["effort"] == "high"
    assert seen[0]["stream"] is True


def _one_fact_short() -> dict:
    plan = _plan()
    plan["stages"][1]["word_budget"] = 44
    plan["word_budget_total"] = 47
    return plan


def test_t1_repair_facts_from_complete_fourth_turn_without_full_regeneration(tmp_path):
    """Real-world recovery: three 32k truncations and a complete 4th near miss."""
    opts = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"ebook", "thumbnail": b"thumb",
        "api_key": "fake", "stop": "T1",
    }
    calls = []

    def provider(*, api_key, system, messages):
        calls.append((system, copy.deepcopy(messages)))
        if len(calls) <= 3:
            return {
                "model": title_flow.MODEL, "stop_reason": "max_tokens",
                "content": [{"type": "thinking", "thinking": "long", "signature": "s"}],
                "usage": {"input_tokens": 500, "output_tokens": 32000},
            }
        if len(calls) == 4:
            return _response(_one_fact_short())
        assert system == title_flow._T1_FACT_REPAIR_SYSTEM
        assert len(calls) == 5
        request = json.loads(messages[0]["content"])
        assert request["missing_facts_by_stage"] == {"S02": 1}
        assert request["stage_outline"][1]["facts"] == ["A different fact."]
        assert request["stage_outline"][0]["facts"] == []
        return _response({
            "facts_by_stage": [{
                "stage_id": "S02",
                "facts": ["The fictional message enters the recipient's inbox first."],
            }],
        })

    expected_raw = _response(_one_fact_short())
    # The legacy retry allowance is discovered only after the third paid
    # response has been checkpointed by a previous invocation.
    with pytest.raises(title_flow.TitleContractError, match="3 attempts exhausted"):
        title_flow.run(**opts, provider=provider)
    assert len(calls) == 3
    result, nothing = title_flow.run(**opts, provider=provider)
    assert nothing is None
    assert len(calls) == 5
    assert len(result["stages"][1]["facts"]) == 2
    assert result["stages"][1]["facts"][0] == "A different fact."
    title_flow.validate_plan(result)
    t1_root = tmp_path / "T1"
    assert title_flow.read(t1_root / "response_attempt_4.json") == expected_raw
    assert (t1_root / "fact_repair" / "input.json").is_file()
    assert (t1_root / "fact_repair" / "response.json").is_file()
    assert title_flow.read(t1_root / "fact_repair" / "output.json") == result
    assert not (t1_root / "fact_repair" / "inflight.json").exists()

    title_flow.run(
        **opts, provider=lambda **kw: pytest.fail("No new paid T1 request permitted")
    )
    (t1_root / "output.json").unlink()
    recovered, _ = title_flow.run(
        **opts, provider=lambda **kw: pytest.fail("Checkpoint replay should be local")
    )
    assert recovered == result
    assert len(calls) == 5


def test_t1_narrow_repair_uncertain_outcome_keeps_inflight_checkpoint(tmp_path):
    opts = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"ebook", "thumbnail": b"thumb",
        "api_key": "fake", "stop": "T1",
    }
    t1_calls = []

    def provider(*, api_key, system, messages):
        t1_calls.append(system)
        if len(t1_calls) == 1:
            return _response(_one_fact_short())
        assert system == title_flow._T1_FACT_REPAIR_SYSTEM
        raise title_flow.ClaudeAPIError("uncertain patch response")

    with pytest.raises(title_flow.ClaudeAPIError, match="uncertain patch"):
        title_flow.run(**opts, provider=provider)
    assert len(t1_calls) == 2
    assert (tmp_path / "T1" / "fact_repair" / "inflight.json").is_file()
    assert not (tmp_path / "T1" / "output.json").exists()
    with pytest.raises(title_flow.TitleContractError, match="outcome uncertain"):
        title_flow.run(
            **opts,
            provider=lambda **kw: pytest.fail("Ambiguous patch must never be re-sent"),
        )


def test_t1_repair_rejects_invented_fields_and_duplicate_facts():
    original = _one_fact_short()
    deficits = title_flow._t1_repairable_fact_deficits(original)
    assert deficits == {"S02": 1}
    bad_patches = [
        {"facts_by_stage": [{"stage_id": "S02",
                             "facts": ["A different fact."]}]},
        {"facts_by_stage": [{"stage_id": "S02",
                             "facts": ["Fresh detail."]}], "narrative_core": {}},
        {"facts_by_stage": [{"stage_id": "S01",
                             "facts": ["Fresh detail."]}]},
    ]
    for patch in bad_patches:
        with pytest.raises(title_flow.TitleContractError):
            title_flow._apply_t1_fact_patch(original, patch, deficits)
    assert original == _one_fact_short()


def test_t1_no_expensive_patch_for_structural_or_large_deficits():
    plan = _one_fact_short()
    plan["stages"][1]["word_budget"] = 155
    plan["word_budget_total"] = 158
    assert title_flow._t1_repairable_fact_deficits(plan) == {}
    plan = _one_fact_short()
    plan["macro_revelation"]["stage_id"] = "S03"
    assert title_flow._t1_repairable_fact_deficits(plan) == {}


def test_t1_repair_transport_uses_medium_effort_and_small_ceiling():
    captured = []

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "{}"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 2}},
                {"type": "message_stop"},
            ]
            return iter(
                [("data: " + json.dumps(event)).encode() for event in events]
            )

    def transport(request, timeout):
        captured.append(json.loads(request.data))
        return FakeStream()

    title_flow.call_claude(
        api_key="fake",
        system=title_flow._T1_FACT_REPAIR_SYSTEM,
        messages=[{"role": "user", "content": "A short fact patch."}],
        max_tokens=title_flow._T1_FACT_REPAIR_MAX_OUTPUT_TOKENS,
        effort=title_flow._T1_FACT_REPAIR_EFFORT,
        transport=transport,
    )
    assert captured[0]["max_tokens"] == 8192
    assert captured[0]["output_config"]["effort"] == "medium"


def test_t1_repair_across_multiple_stages_over_old_four_fact_limit(tmp_path):
    """Repair all deficits after one full response, without paid rewrites."""
    plan = _plan()
    plan["stages"][0]["word_budget"] = 66
    plan["stages"][1]["word_budget"] = 66
    plan["stages"][2]["word_budget"] = 44
    plan["word_budget_total"] = 176
    expected = {"S01": 2, "S02": 2, "S03": 1}
    analysis = title_flow._t1_fact_repair_diagnostics(plan)
    assert analysis == {"deficits": expected, "reason": None}
    assert title_flow._t1_repairable_fact_deficits(plan) == expected
    response = _response(plan)
    opts = {
        "output": tmp_path, "avatar": "Uncle_Enzo", "title": "Wrong Number",
        "ebook": b"ebook", "thumbnail": b"thumb", "api_key": "fake", "stop": "T1",
    }
    calls = []

    def provider(*, api_key, system, messages):
        calls.append(system)
        if len(calls) == 1:
            return response
        assert system == title_flow._T1_FACT_REPAIR_SYSTEM
        request = json.loads(messages[0]["content"])
        assert request["missing_facts_by_stage"] == expected
        return _response({
            "facts_by_stage": [{
                "stage_id": stage, "facts": [
                    f"The {stage} detail number {n} is shown on the phone."
                    for n in range(count)
                ],
            } for stage, count in expected.items()],
        })

    fixed, _ = title_flow.run(**opts, provider=provider)
    assert len(calls) == 2
    assert [len(stage["facts"]) for stage in fixed["stages"]] == [3, 3, 2]
    assert fixed["word_budget_total"] == 176
    title_flow.validate_plan(fixed)


def test_t1_repair_diagnostic_explains_other_contract_failure():
    plan = _one_fact_short()
    plan["macro_revelation"]["stage_id"] = "S03"
    analysis = title_flow._t1_fact_repair_diagnostics(plan)
    assert analysis["deficits"] == {"S02": 1}
    assert "macro_revelation" in analysis["reason"]
    assert title_flow._t1_repairable_fact_deficits(plan) == {}


def test_t1_repair_diagnostic_refuses_rewriting_most_of_plan():
    plan = _one_fact_short()
    plan["stages"][1]["word_budget"] = 155
    plan["word_budget_total"] = 158
    analysis = title_flow._t1_fact_repair_diagnostics(plan)
    assert analysis["deficits"] == {"S02": 6}
    assert "Too many" in analysis["reason"]
    assert title_flow._t1_repairable_fact_deficits(plan) == {}


def test_v32_explicit_200_word_example_accepts_nine_facts():
    """The supplied prompt says 200 words => at least NINE facts, not ten."""
    plan = _plan()
    plan["stages"][1]["word_budget"] = 200
    plan["word_budget_total"] = 203
    plan["stages"][1]["facts"] = [f"Known fact {n}." for n in range(9)]
    title_flow.validate_plan(plan)
    assert title_flow._t1_required_fact_count(200) == 9
    plan["stages"][1]["facts"].pop()
    with pytest.raises(title_flow.TitleContractError, match="requires 9 facts"):
        title_flow.validate_plan(plan)


def test_v32_closing_return_moved_to_actual_final_stage_offline(tmp_path):
    plan = _plan()
    plan["stages"][1]["markers"].append("closing_return")
    plan["stages"][2]["markers"].remove("closing_return")
    original = copy.deepcopy(plan)
    with pytest.raises(title_flow.TitleContractError, match="closing_return"):
        title_flow.validate_plan(plan)

    changes = [
        {"stage_id": "S02", "action": "remove_misplaced_closing_return"},
        {"stage_id": "S03", "action": "add_closing_return"},
    ]
    corrected, applied = title_flow._t1_normalize_closing_marker(plan)
    assert applied == changes
    assert plan == original
    title_flow.validate_plan(corrected)
    assert title_flow._t1_fact_repair_diagnostics(plan) == {
        "deficits": {}, "reason": None,
    }
    assert title_flow._t1_repairable_fact_deficits(plan) == {}

    opts = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"ebook",
        "thumbnail": b"thumb", "api_key": "fake", "stop": "T1",
    }
    calls = []
    def provider(*, api_key, system, messages):
        calls.append(system)
        return _response(plan)
    output, _ = title_flow.run(**opts, provider=provider)
    assert len(calls) == 1
    assert output == corrected
    assert title_flow.read(
        tmp_path / "T1" / "response_attempt_1.json"
    ) == _response(plan)
    audit = title_flow.read(tmp_path / "T1" / "marker_repair.json")
    assert audit["changes"] == changes
    assert (tmp_path / "T1" / "output.json").is_file()
    title_flow.run(
        **opts, provider=lambda **kw: pytest.fail("No paid replay allowed")
    )


def test_t1_v32_combined_closing_and_fact_repair_uses_one_small_patch(tmp_path):
    """Regression for user's S05=9/200 and misplaced S12 marker."""
    plan = _plan()
    stages = [
        {
            "id": f"S{index:02d}",
            "kind": "hook" if index == 1 else "closing" if index == 13 else "chain",
            "markers": ["hook"] if index == 1 else (
                ["macro_revelation"] if index == 2 else (
                    ["closing_return"] if index == 12 else []
                )
            ),
            "word_budget": 200,
            "facts": [f"S{index:02d} known fact {j}." for j in range(
                8 if index in {6, 7, 9} else 9
            )],
        } for index in range(1, 14)
    ]
    plan["stages"] = stages
    plan["word_budget_total"] = 13 * 200
    original = copy.deepcopy(plan)
    assert title_flow._t1_required_fact_count(200) == 9
    diagnostic = title_flow._t1_fact_repair_diagnostics(plan)
    assert diagnostic == {
        "deficits": {"S06": 1, "S07": 1, "S09": 1}, "reason": None,
    }
    assert plan == original

    calls = []
    opts = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"ebook",
        "thumbnail": b"thumb", "api_key": "fake", "stop": "T1",
    }

    def provider(*, api_key, system, messages):
        calls.append(system)
        if len(calls) == 1:
            return _response(plan)
        assert system == title_flow._T1_FACT_REPAIR_SYSTEM
        request = json.loads(messages[0]["content"])
        assert request["missing_facts_by_stage"] == {
            "S06": 1, "S07": 1, "S09": 1,
        }
        assert request["stage_outline"][4]["facts"] == []
        return _response({
            "facts_by_stage": [
                {"stage_id": stage, "facts": [
                    f"The {stage} phone screen shows one new confirmed field."
                ]}
                for stage in ("S06", "S07", "S09")
            ]
        })

    fixed, _ = title_flow.run(**opts, provider=provider)
    assert len(calls) == 2
    title_flow.validate_plan(fixed)
    assert fixed["stages"][4]["facts"] == original["stages"][4]["facts"]
    assert "closing_return" not in fixed["stages"][11]["markers"]
    assert "closing_return" in fixed["stages"][12]["markers"]
    assert "closing_return" in plan["stages"][11]["markers"]
    assert title_flow.read(tmp_path / "T1" / "marker_repair.json")["changes"]
    assert (tmp_path / "T1" / "fact_repair" / "output.json").is_file()


def test_t1_closing_marker_fix_refuses_ambiguity_or_other_issues():
    plan = _plan()
    plan["stages"][1]["markers"].append("closing_return")
    # Two closing_return markers make the position ambiguous.
    corrected, changes = title_flow._t1_normalize_closing_marker(plan)
    assert corrected == plan and changes == []
    plan = _plan()
    plan["stages"][-1]["kind"] = "chain"
    corrected, changes = title_flow._t1_normalize_closing_marker(plan)
    assert corrected == plan and changes == []



def test_t1_resume_keeps_historical_overstrict_rejection_immutable(tmp_path):
    """The user's old S05 9/200 rejection must not block corrected replay."""
    plan = _plan()
    plan["stages"][1]["word_budget"] = 200
    plan["word_budget_total"] = 203
    plan["stages"][1]["facts"] = [f"Relevant fact {n}." for n in range(9)]
    plan["stages"][1]["markers"].append("closing_return")
    plan["stages"][-1]["markers"].remove("closing_return")
    response = _response(plan)
    old_reason = "T1 stage 2 only 9 facts for 200 words (v32: 1/22)"
    assert title_flow._legacy_t1_fact_rejection(plan) == old_reason
    root = tmp_path / "T1"
    title_flow.write(root / "response_attempt_1.json", response)
    title_flow.write(root / "rejection_attempt_1.json", {"reason": old_reason})
    previous_bytes = (root / "response_attempt_1.json").read_bytes()
    rejected_bytes = (root / "rejection_attempt_1.json").read_bytes()
    opts = {
        "output": tmp_path, "avatar": "Uncle_Enzo",
        "title": "Wrong Number", "ebook": b"cover",
        "thumbnail": b"thumb", "api_key": "fake", "stop": "T1",
    }

    repaired, remaining = title_flow.run(
        **opts, provider=lambda **kwargs: pytest.fail("No paid replay needed")
    )
    assert remaining is None
    title_flow.validate_plan(repaired)
    assert (root / "response_attempt_1.json").read_bytes() == previous_bytes
    assert (root / "rejection_attempt_1.json").read_bytes() == rejected_bytes
    assert repaired["stages"][1]["facts"] == plan["stages"][1]["facts"]
    assert (root / "marker_repair.json").is_file()
    audit = title_flow.read(root / "revalidation_attempt_1.json")
    assert audit["original_reason"] == old_reason
    assert "closing_return" in audit["current_reason"]
    title_flow.run(
        **opts, provider=lambda **kwargs: pytest.fail("Fully cached T1 replay")
    )


def test_t1_revalidation_rejects_unproven_historical_reason(tmp_path):
    plan = _plan()
    plan["stages"][1]["markers"].append("closing_return")
    plan["stages"][-1]["markers"].remove("closing_return")
    root = tmp_path / "T1"
    title_flow.write(root / "response_attempt_1.json", _response(plan))
    title_flow.write(root / "rejection_attempt_1.json", {
        "reason": "Some other unverified rejection",
    })
    with pytest.raises(title_flow.TitleContractError, match="checkpoint changed"):
        title_flow.run(
            output=tmp_path, avatar="Uncle_Enzo",
            title="Wrong Number", ebook=b"cover",
            thumbnail=b"thumb", api_key="fake", stop="T1",
            provider=lambda **kw: pytest.fail("Never resend a paid T1 request"),
        )



def test_t1_diagnostic_zero_deficits_valid_plan_is_not_false_negative():
    """No missing facts with valid markers means the plan is already valid."""
    plan = _plan()
    assert title_flow._t1_fact_repair_diagnostics(plan) == {
        "deficits": {}, "reason": None,
    }
    assert title_flow._t1_repairable_fact_deficits(plan) == {}


def test_t1_diagnostic_zero_deficits_reports_actual_unrepaired_marker_error():
    """No-op closing normalization must not disguise a structural T1 error."""
    plan = _plan()
    plan["stages"][-1]["markers"].remove("closing_return")
    plan["stages"][-1]["kind"] = "chain"
    # No facts are missing and marker normalization must refuse a
    # structural change to stage.kind instead of pretending the plan is fine.
    checked, changes = title_flow._t1_normalize_closing_marker(plan)
    assert changes == []
    assert checked == plan
    diagnosis = title_flow._t1_fact_repair_diagnostics(plan)
    assert diagnosis["deficits"] == {}
    assert "closing" in diagnosis["reason"]
