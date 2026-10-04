"""V4's compiler may translate V3 decisions, never direct new ones."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from ai_video_factory.bots import professional_voice as pv


def _block():
    return {
        "block_id": 1, "type": "development",
        "emotional_entry": "curiosity", "emotional_exit": "recognition",
        "span": {"first_words": "Hola", "last_words": "realidad"},
        "beats": [
            {"beat_id": "1A", "text": "Hola, mundo. "},
            {"beat_id": "1B", "text": "Esa es la realidad."},
        ],
    }


def _artistic():
    return pv.V3(
        pipeline_stage="V3", block_id=1,
        block_performance={
            "register": "Grounded conversational storyteller",
            "pace_character": "Broad opening, then precise",
            "intensity_arc": "From warmth toward recognition",
        },
        beat_deliveries=[
            {"beat_id": "1A", "delivery": [
                {"speak": "Hola, ", "delivery_quality": "Warm opening",
                 "ending_behavior": "carry", "stress": [],
                 "vocal_intents": [{"at": 0, "behavior": "inhale"}]},
                {"pause": "beat", "pause_function": "Let the greeting register"},
                {"speak": "mundo. ", "delivery_quality": "Give mundo semantic focus",
                 "ending_behavior": "resolve", "stress": ["mundo"],
                 "vocal_intents": []},
            ]},
            {"beat_id": "1B", "delivery": [
                {"speak": "Esa es la realidad.",
                 "delivery_quality": "Measured recognition",
                 "ending_behavior": "land", "stress": [],
                 "vocal_intents": []},
            ]},
        ],
    )


def _compiled():
    return pv.V4(
        pipeline_stage="V4", block_id=1,
        audio_profile="Grounded conversational storyteller",
        directors_notes=["Begin broadly, narrow toward precise recognition."],
        beat_deliveries=[
            {"beat_id": "1A", "segments": [
                {"source_item_index": 0, "speak": "Hola, ",
                 "style": "Warm opening; keep the thought open; Let the greeting register",
                 "ending_behavior": "carry", "stress": [],
                 "events": [
                     {"at": 0, "tag": "breath", "source_kind": "vocal_intent",
                      "source_item_index": 0, "source_at": 0},
                     {"at": 6, "tag": "short pause", "source_kind": "pause",
                      "source_item_index": 1, "source_at": None,
                      "pause_direction": "Let the greeting register"},
                 ]},
                {"source_item_index": 2, "speak": "mundo. ",
                 "style": "Emphasize mundo, resolve naturally",
                 "ending_behavior": "resolve", "stress": ["mundo"], "events": []},
            ]},
            {"beat_id": "1B", "segments": [
                {"source_item_index": 0, "speak": "Esa es la realidad.",
                 "style": "Measured recognition; land decisively",
                 "ending_behavior": "land", "stress": [], "events": []},
            ]},
        ],
    )


def test_compiler_preserves_distinct_pause_spoken_time_and_exact_stress():
    source, direction, compiler = _block(), _artistic(), _compiled()
    pv._check_v3(direction, source)
    pv._check_v4(compiler, source, direction)
    turns = pv._v4_turns(compiler)
    assert turns[0]["text"] == "<breath>Hola, <short pause>"
    assert turns[1]["text"] == "mundo. "
    assert "mundo" in turns[1]["style"]
    assert "Broadly" in turns[0]["style"] or "broadly" in turns[0]["style"]
    assert turns[2]["text"] == "Esa es la realidad."
    assert "Let the greeting register" in turns[0]["style"]
    assert "Grounded conversational" not in turns[1]["style"]


@pytest.mark.parametrize(("change", "error"), [
    (lambda value: value.beat_deliveries[0].segments[0].events.pop(),
     "did not preserve"),
    (lambda value: value.beat_deliveries[0].segments[1].stress.clear(),
     "changed source words, stress"),
    (lambda value: setattr(
        value.beat_deliveries[0].segments[0].events[1], "tag", "gasp"
    ), "changed an explicit pause"),
    (lambda value: setattr(
        value.beat_deliveries[0].segments[0].events[1], "at", 2
    ), "inside a word|moved an explicit"),
    (lambda value: value.beat_deliveries[1].segments[0].events.append(
        pv.V4Event(
            at=0, tag="gasp", source_kind="vocal_intent",
            source_item_index=0, source_at=0,
        )
    ), "changed or invented"),
])
def test_compiler_rejects_lost_or_invented_decisions(change, error):
    result = copy.deepcopy(_compiled())
    change(result)
    with pytest.raises(pv.VoicePlanError, match=error):
        pv._check_v4(result, _block(), _artistic())


def test_v3_rejects_unjustified_stress_or_invented_words():
    result = _artistic()
    result.beat_deliveries[0].delivery[2].stress = ["MUNDO"]
    with pytest.raises(pv.VoicePlanError, match="stress"):
        pv._check_v3(result, _block())
    result = _artistic()
    result.beat_deliveries[0].delivery[2].speak = "mundo inventado."
    with pytest.raises(pv.VoicePlanError, match="verbatim"):
        pv._check_v3(result, _block())


def test_existing_legacy_v3_checkpoint_skips_new_v4_without_new_paid_calls(tmp_path):
    core, block = {"central_question": "Why?", "final_answer": "Because."}, _block()
    rhythm = pv.V1(
        pipeline_stage="V1", curve="A recognition follows the greeting",
        block_rhythms=[
            {"block_id": 1, "phase": "Reveals the situation",
             "viewer_trajectory": "Moves into recognition",
             "tempo": "measured", "pressure": "moderate",
             "contrast": "Clear opening before a final conclusion"},
        ], baseline_reconsiderations=[],
    )
    intent = pv.V2(
        pipeline_stage="V2", block_id=1, beat_intents=[
            {"beat_id": beat["beat_id"], "function": "Develops the point",
             "understanding_delta": "Learns the situation",
             "viewer_trajectory": "Moves toward recognition",
             "tempo_modulation": "at", "pressure_modulation": "at"}
            for beat in block["beats"]
        ], landing_beat_id="1B", baseline_misfit=[],
    )
    old_direction = pv.LegacyV3(
        pipeline_stage="V3", block_id=1, beat_deliveries=[
            {"beat_id": beat["beat_id"], "segments": [
                {"speak": beat["text"], "style": "Original professional style",
                 "ending_behavior": "carry", "events": []},
            ]}
            for beat in block["beats"]
        ],
    )
    inputs = {
        "narrative_core": core, "curve": rhythm.curve,
        "current_block": block,
        "block_rhythm": rhythm.block_rhythms[0].model_dump(),
        "beat_intents": [
            pv._resolved(rhythm.block_rhythms[0], beat) for beat in intent.beat_intents
        ],
        "landing_beat_id": intent.landing_beat_id,
    }
    checkpoint = tmp_path / "V3" / "block_1"
    checkpoint.mkdir(parents=True)
    (checkpoint / "input.json").write_text(json.dumps({"payload": inputs}))
    (checkpoint / "output.json").write_text(old_direction.model_dump_json())
    class Provider:
        calls = []
        async def generate_structured_with_response(self, *, output_type, **_):
            self.calls.append(output_type.__name__)
            value = rhythm if output_type is pv.V1 else intent
            return value, SimpleNamespace(id=str(len(self.calls)), model="test", usage=None)
    provider = Provider()
    v1, v2, v3, v4 = asyncio.run(pv.plan_professional_voice(
        core=core, blocks=[block], output=tmp_path, provider=provider, model="test",
    ))
    assert (v1, v2, v3, v4) == (rhythm, [intent], [old_direction], [])
    assert provider.calls == ["V1", "V2"]
    assert not (tmp_path / "V4").exists()


def test_v4_rejects_pause_without_artistic_function_in_gemini_style():
    compiler = _compiled()
    event = compiler.beat_deliveries[0].segments[0].events[1]
    event.pause_direction = None
    with pytest.raises(pv.VoicePlanError, match="audible purpose"):
        pv._check_v4(compiler, _block(), _artistic())
    compiler = _compiled()
    compiler.beat_deliveries[0].segments[0].style = "Warm opening; keep open"
    with pytest.raises(pv.VoicePlanError, match="audible purpose"):
        pv._check_v4(compiler, _block(), _artistic())


def test_v3_coincident_human_behavior_and_explicit_pause_preserve_source_order():
    direction = _artistic()
    direction.beat_deliveries[0].delivery[0].vocal_intents = [
        pv.VocalIntent(at=0, behavior="inhale"),
        pv.VocalIntent(at=6, behavior="exhale"),
    ]
    compiler = _compiled()
    compiler.beat_deliveries[0].segments[0].events.insert(1, pv.V4Event(
        at=6, tag="exhales", source_kind="vocal_intent",
        source_item_index=0, source_at=6,
    ))
    pv._check_v3(direction, _block())
    pv._check_v4(compiler, _block(), direction)
    assert pv._v4_turns(compiler)[0]["text"] == (
        "<breath>Hola, <exhales><short pause>"
    )


def test_legacy_checkpoint_detection_is_structural_not_prose_substring(tmp_path):
    # A new-format delivery quality mentioning 'segments' is NOT old V3.
    direction = _artistic()
    direction.beat_deliveries[0].delivery[0].delivery_quality = (
        "Join the segments naturally"
    )
    saved = tmp_path / "V3" / "block_1"
    saved.mkdir(parents=True)
    (saved / "output.json").write_text(direction.model_dump_json())
    # A saved new V3 result must not force the whole run onto LegacyV3.
    snapshot = json.loads((saved / "output.json").read_text())
    assert "segments" in (saved / "output.json").read_text()
    assert "delivery" in snapshot["beat_deliveries"][0]
    assert "segments" not in snapshot["beat_deliveries"][0]


def test_silent_breath_class_never_creates_unrequested_audible_inhale():
    direction = _artistic()
    direction.beat_deliveries[0].delivery[1] = pv.Pause(
        pause="breath", pause_function="Silently re-gather before resuming"
    )
    compiler = _compiled()
    destination = compiler.beat_deliveries[0].segments[0]
    destination.events[1].pause_direction = "Silently re-gather before resuming"
    destination.style = "Warm opening; Silently re-gather before resuming"
    pv._check_v4(compiler, _block(), direction)
    destination.events[1].tag = "breath"
    with pytest.raises(pv.VoicePlanError, match="changed an explicit pause class"):
        pv._check_v4(compiler, _block(), direction)
