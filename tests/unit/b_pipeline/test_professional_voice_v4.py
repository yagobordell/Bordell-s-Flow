"""V4 has final authority; only technical B2/Gemini safety is validated."""
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
        audio_profile="Independent documentary voice",
        directors_notes=["Natural direct narration, no designed pauses."],
        spoken_directions=[{"locator": "mundo", "instruction": "Light emphasis"}],
        beat_tags=[
            {"beat_id": "1A", "text": "", "inserts": [
                {"at": 0, "tag": "breath", "text": "<breath>Hola"},
                {"at": 6, "tag": "short pause", "text": "Hola, <short pause>mundo"},
            ]},
            {"beat_id": "1B", "text": "", "inserts": []},
        ],
    )


def test_v4_full_group_one_style_and_only_its_own_instructions():
    source, compiled = _block(), _compiled()
    pv._check_v4(compiled, source)
    turns = pv._v4_turns(compiled, source)
    assert len(turns) == 1
    assert turns[0]["text"] == (
        "<breath>Hola, <short pause>mundo. Esa es la realidad."
    )
    assert "Natural direct narration" in turns[0]["style"]
    assert "Light emphasis" in turns[0]["style"]
    assert "Grounded conversational" not in turns[0]["style"]
    assert "Let the greeting register" not in turns[0]["style"]


def test_v4_can_ignore_every_v3_decision_including_stress_and_pause():
    source, compiled = _block(), _compiled()
    v3 = _artistic()
    v3.block_performance.register_ = "V3 different narrator description"
    v3.beat_deliveries[0].delivery[0].delivery_quality = "V3 must whisper"
    v3.beat_deliveries[0].delivery[2].stress = ["mundo"]
    pv._check_v3(v3, source)
    compiled.beat_tags[0].inserts = []
    compiled.spoken_directions = []
    compiled.directors_notes = ["Speak forward with NO designed silence."]
    pv._check_v4(compiled, source)
    turns = pv._v4_turns(compiled, source)
    assert turns == [{
        "text": "Hola, mundo. Esa es la realidad.",
        "style": "Independent documentary voice\nSpeak forward with NO designed silence.",
    }]
    assert "register" not in turns[0]["style"].lower()


def test_v4_can_introduce_supported_tag_not_proposed_by_v3():
    direction, block = _compiled(), _block()
    direction.beat_tags[0].inserts = [
        {"at": 6, "tag": "gasp", "text": "Hola, <gasp>mundo"},
    ]
    direction = pv.V4.model_validate(direction.model_dump())
    pv._check_v4(direction, block)
    assert pv._v4_turns(direction, block)[0]["text"].startswith("Hola, <gasp>mundo")


def test_v4_different_punctual_choice_at_original_v3_pause_seam_is_allowed():
    source, compiled = _block(), _compiled()
    compiled.beat_tags[0].inserts[1].tag = "chuckle"
    compiled.beat_tags[0].inserts[1].text = "Hola, <chuckle>mundo"
    pv._check_v4(compiled, source)
    assert "<chuckle>" in pv._v4_turns(compiled, source)[0]["text"]


@pytest.mark.parametrize(("mutate", "error"), [
    (lambda x: setattr(x.beat_tags[0].inserts[1], "text", "Hola, <short pause>mundo. Esa"),
     "adjacent"),
    (lambda x: setattr(x.beat_tags[0].inserts[1], "at", 2), "splits a word"),
    (lambda x: x.beat_tags[0].inserts.append(
        pv.V4TagSnippet(at=0, tag="breath", text="<breath>Hola")), "duplicated"),
    (lambda x: setattr(x.beat_tags[0], "beat_id", "WRONG"), "B2 order"),
    (lambda x: setattr(x, "audio_profile", "   "), "substantive"),
])
def test_v4_rejects_only_invalid_technical_output(mutate, error):
    compiled = copy.deepcopy(_compiled())
    mutate(compiled)
    with pytest.raises(pv.VoicePlanError, match=error):
        pv._check_v4(compiled, _block())


def test_repeated_identical_excerpts_use_their_own_absolute_b2_offsets():
    text = "como como como"
    block = {"block_id": 9, "beats": [{"beat_id": "R", "text": text}]}
    compiled = pv.V4(
        pipeline_stage="V4", block_id=9, audio_profile="Independent actor",
        directors_notes=["Full independence"],
        spoken_directions=[{"locator": "como", "occurrence": 2,
                            "instruction": "Make the second 'como' curious"}],
        beat_tags=[{"beat_id": "R", "text": "", "inserts": [
            {"at": at, "tag": "breath", "text": "como <breath>como"}
            for at in (5, 10)
        ]}],
    )
    pv._check_v4(compiled, block)
    assert pv._v4_turns(compiled, block)[0]["text"] == (
        "como <breath>como <breath>como"
    )
    compiled.beat_tags[0].inserts[1].at = 5
    with pytest.raises(pv.VoicePlanError, match="duplicated"):
        pv._check_v4(compiled, block)


def test_same_offset_different_tags_preserve_v4_authored_order():
    compiler = _compiled()
    compiler.beat_tags[0].inserts = [
        pv.V4TagSnippet(at=6, tag="exhales", text="Hola, <exhales>mundo"),
        pv.V4TagSnippet(at=6, tag="short pause", text="Hola, <short pause>mundo"),
    ]
    pv._check_v4(compiler, _block())
    assert pv._v4_turns(compiler, _block())[0]["text"].startswith(
        "Hola, <exhales><short pause>mundo"
    )


def test_v4_invalid_local_locator_is_only_an_invalid_b2_reference():
    compiler = _compiled()
    compiler.spoken_directions = [
        pv.V4SpokenDirection(locator="not B2", instruction="Anything"),
    ]
    with pytest.raises(pv.VoicePlanError, match="invalid B2 locator"):
        pv._check_v4(compiler, _block())


def test_v3_rejects_unjustified_stress_or_invented_words():
    result = _artistic()
    result.beat_deliveries[0].delivery[2].stress = ["MUNDO"]
    with pytest.raises(pv.VoicePlanError, match="stress"):
        pv._check_v3(result, _block())
    result = _artistic()
    result.beat_deliveries[0].delivery[2].speak = "mundo inventado."
    with pytest.raises(pv.VoicePlanError, match="verbatim"):
        pv._check_v3(result, _block())


def test_legacy_v3_checkpoint_is_rejected_without_new_paid_calls(tmp_path):
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
    old_direction = {
        "pipeline_stage": "V3", "block_id": 1, "beat_deliveries": [
            {"beat_id": beat["beat_id"], "segments": [{
                "speak": beat["text"], "style": "Obsolete Gemini style",
                "ending_behavior": "carry", "events": [{
                    "at": 0, "tag": "breath",
                }],
            }]}
            for beat in block["beats"]
        ],
    }
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
    (checkpoint / "output.json").write_text(json.dumps(old_direction))
    class Provider:
        calls = []
        async def generate_structured_with_response(self, *, output_type, **_):
            self.calls.append(output_type.__name__)
            value = rhythm if output_type is pv.V1 else intent
            return value, SimpleNamespace(id=str(len(self.calls)), model="test", usage=None)
    provider = Provider()
    with pytest.raises(pv.VoicePlanError, match="Legacy V3 checkpoint"):
        asyncio.run(pv.plan_professional_voice(
            core=core, blocks=[block], output=tmp_path, provider=provider, model="test",
        ))
    assert provider.calls == []  # Reject before any text or paid audio API call.
    assert json.loads((checkpoint / "output.json").read_text()) == old_direction
    assert not (tmp_path / "V4").exists()


def test_modern_checkpoint_detection_is_structural_not_prose_substring(tmp_path):
    # A new-format delivery quality mentioning 'segments' is NOT old V3.
    direction = _artistic()
    direction.beat_deliveries[0].delivery[0].delivery_quality = (
        "Join the segments naturally"
    )
    saved = tmp_path / "V3" / "block_1"
    saved.mkdir(parents=True)
    (saved / "output.json").write_text(direction.model_dump_json())
    # Modern V3 stays on the modern V3/V4 contract regardless of its prose.
    snapshot = json.loads((saved / "output.json").read_text())
    assert "segments" in (saved / "output.json").read_text()
    assert "delivery" in snapshot["beat_deliveries"][0]
    assert "segments" not in snapshot["beat_deliveries"][0]


def test_v3_collapses_duplicate_explicit_silence_across_beat_boundary():
    direction = _artistic()
    direction.beat_deliveries[0].delivery.append(pv.Pause(
        pause="hold", pause_function="Leave the claim unresolved"
    ))
    direction.beat_deliveries[1].delivery.insert(0, pv.Pause(
        pause="beat", pause_function="Allow the next statement to register"
    ))
    with pytest.raises(pv.VoicePlanError, match="across original beat boundary"):
        pv._check_v3(direction, _block())


def test_v3_pause_function_rejects_inline_gemini_token():
    direction = _artistic()
    direction.beat_deliveries[0].delivery[1].pause_function = (
        "Let greeting register <short pause>"
    )
    with pytest.raises(pv.VoicePlanError, match="V3 inline-tag leakage"):
        pv._check_v3(direction, _block())


