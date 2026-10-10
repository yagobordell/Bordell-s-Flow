"""Regression tests for immutable E1/E2 editorial contracts and speech timing."""
from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from ai_video_factory.editorial.compiler import (
    EditorialValidationError,
    compile_plan,
    resolve_anchor,
)
from ai_video_factory.editorial.contracts import (
    E1Output,
    E2Output,
    SpokenAnchor,
)
from ai_video_factory.editorial.directors import run_e1
from ai_video_factory.editorial.word_index import build_word_index


def _one():
    return E1Output.model_validate({
        "schema_version": "e1-v1",
        "interventions": [{
            "id": "cloned-voice",
            "kind": "progressive_text",
            "elements": [
                {"text": "CLONED", "anchor": {"beat_id": "1A", "phrase": "cloned"}},
                {"text": "VOICE", "anchor": {"beat_id": "1A", "phrase": "voice"}},
            ],
            "end_beat_id": "1A",
            "layout_intent": {"placement": "center", "scale": "large", "color": "#FFFFFF"},
            "animation": "fade",
        }],
    })


def _e2():
    return E2Output.model_validate({
        "schema_version": "e2-v1",
        "decisions": [{
            "id": "cloned-voice", "status": "approved",
            "reason": "Available central area",
            "x": 0.1, "y": 0.2, "width": 0.8, "height": 0.3,
            "font_size_px": 84, "color": "#FFFFFF",
            "stroke_color": "#000000", "stroke_width_px": 2, "align": "center",
        }],
    })


def _index():
    return {
        "1A": [
            {"token": "cloned", "start_seconds": 0.5, "end_seconds": 0.8},
            {"token": "voice", "start_seconds": 0.9, "end_seconds": 1.2},
        ],
    }


def _timeline():
    return {
        "fps": 24, "duration_seconds": 2.0,
        "beats": [{"beat_id": "1A", "start_frame": 0, "end_frame": 48}],
    }


def test_progressive_text_is_resolved_by_original_stt_words():
    result = compile_plan(_one(), _e2(), _index(), _timeline())
    assert result["duration_frames"] == 48
    assert result["events"][0]["elements"] == [
        {"text": "CLONED", "frame": 12},
        {"text": "VOICE", "frame": 22},
    ]


def test_suppressed_text_never_enters_remotion():
    e2 = _e2()
    e2.decisions[0].status = "suppressed"
    assert compile_plan(_one(), e2, _index(), _timeline())["events"] == []


def test_unknown_and_ambiguous_spoken_anchors_fail_closed():
    with pytest.raises(EditorialValidationError):
        resolve_anchor(SpokenAnchor(beat_id="1B", phrase="cloned"), _index())
    with pytest.raises(EditorialValidationError):
        resolve_anchor(SpokenAnchor(beat_id="1A", phrase="unknown"), _index())
    with pytest.raises(EditorialValidationError):
        resolve_anchor(
            SpokenAnchor(beat_id="1A", phrase="voice", occurrence=2), _index(),
        )


def test_e2_must_cover_exactly_e1_decisions():
    with pytest.raises(EditorialValidationError):
        compile_plan(_one(), E2Output(schema_version="e2-v1", decisions=[]),
                     _index(), _timeline())


def test_geometric_bounds_and_hex_colors_are_strict():
    bad = _e2().model_dump()
    bad["decisions"][0]["x"] = 0.9
    with pytest.raises(ValidationError):
        E2Output.model_validate(bad)
    bad["decisions"][0]["x"] = 0.1
    bad["decisions"][0]["color"] = "not-a-color"
    with pytest.raises(ValidationError):
        E2Output.model_validate(bad)


def test_generated_plan_rejects_duplicate_effect_ids():
    item = _one().interventions[0].model_dump()
    with pytest.raises(ValidationError):
        E1Output.model_validate({
            "schema_version": "e1-v1", "interventions": [item, item],
        })


def test_offline_word_index_uses_verified_transcript_and_original_group_offset(tmp_path):
    transcript = {
        "source_audio_sha256": "fixed-audio-sha",
        "model": "fixed-stt",
        "words": [
            {"word": "cloned", "start": 0.5, "end": 0.8},
            {"word": "voice", "start": 0.9, "end": 1.2},
        ],
    }
    path = tmp_path / "stt.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    audio = {
        "block_start_seconds": [3.0],
        "blocks": [{
            "block_id": 1, "sha256": "fixed-audio-sha",
            "stt": {"model": "fixed-stt", "file": "stt.json", "sha256": sha},
        }],
    }
    plan = {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "text": "Cloned voice."},
    ]}]}
    words = build_word_index(tmp_path, plan, audio)
    assert words["1A"][0]["start_seconds"] == 3.5
    assert words["1A"][1]["end_seconds"] == 4.2


@pytest.mark.asyncio
async def test_e1_cache_reuses_exact_response_without_new_paid_call(tmp_path, monkeypatch):
    from ai_video_factory.editorial import directors

    class Provider:
        calls = 0

        async def generate_structured_with_response(self, **kwargs):
            self.calls += 1
            class Response:
                id = "response-one"
                model = "model-one"
                service_tier = "standard"
                usage = None
            return _one(), Response()

    provider = Provider()
    monkeypatch.setattr(directors, "_instructions", lambda _filename: "fixed")
    plan = {
        "narrative_core": {"central_question": "Question", "final_answer": "Answer"},
        "blocks": [{"block_id": 1, "type": "intro", "beats": [{
            "beat_id": "1A", "text": "cloned voice", "beat_type": "claim",
            "visual_type": "media_image", "prompt": "Person using phone",
        }]}],
    }
    for _ in range(2):
        answer, _key = await run_e1(
            tmp_path, plan, _timeline(), provider=provider, model="model-one",
        )
        assert answer == _one()
    assert provider.calls == 1
