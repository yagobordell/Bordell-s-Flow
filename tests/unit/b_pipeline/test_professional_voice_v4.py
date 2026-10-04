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
        spoken_directions=[
            {"beat_id": "1A", "source_item_index": 0, "locator": "Hola",
             "instruction": "Warm opening; keep the thought open",
             "ending_behavior": "carry", "stress": []},
            {"beat_id": "1A", "source_item_index": 2, "locator": "mundo",
             "instruction": "Emphasize mundo; resolve naturally",
             "ending_behavior": "resolve", "stress": ["mundo"]},
            {"beat_id": "1B", "source_item_index": 0, "locator": "realidad",
             "instruction": "Measured recognition; land decisively",
             "ending_behavior": "land", "stress": []},
        ],
        beat_tags=[
            {"beat_id": "1A", "text": "", "inserts": [
                {"at": 0, "tag": "breath", "text": "<breath>Hola",
                 "source_kind": "vocal_intent",
                 "source_item_index": 0, "source_at": 0},
                {"at": 6, "tag": "short pause", "text": "Hola, <short pause>mundo",
                 "source_kind": "pause", "source_item_index": 1,
                 "source_at": None, "pause_direction": "Let the greeting register"},
            ]},
            {"beat_id": "1B", "text": "", "inserts": []},
        ],
    )


def test_compiler_preserves_distinct_pause_spoken_time_and_exact_stress():
    source, direction, compiler = _block(), _artistic(), _compiled()
    pv._check_v3(direction, source)
    pv._check_v4(compiler, source, direction)
    turns = pv._v4_turns(compiler, source, direction)
    assert len(turns) == 1
    assert turns[0]["text"] == (
        "<breath>Hola, <short pause>mundo. Esa es la realidad."
    )
    assert "mundo" in turns[0]["style"]
    assert "broadly" in turns[0]["style"]
    assert "Let the greeting register" in turns[0]["style"]
    assert "Grounded conversational" in turns[0]["style"]
    assert compiler.beat_tags[1].text == ""
    assert compiler.beat_tags[1].inserts == []


@pytest.mark.parametrize(("change", "error"), [
    (lambda value: value.spoken_directions[1].stress.clear(),
     "exact spoken locator, stress"),
    (lambda value: setattr(value.beat_tags[0].inserts[1], "tag", "gasp"),
     "invented, duplicated or moved"),
    (lambda value: setattr(value.beat_tags[0].inserts[1], "at", 2),
     "invented, duplicated or moved"),
    (lambda value: value.beat_tags[1].inserts.append(
        pv.V4TagSnippet(at=0, tag="gasp", text="<gasp>Esa",
                        source_kind="vocal_intent",
                        source_item_index=0, source_at=0)
    ), "invented, duplicated or moved"),
])
def test_compiler_rejects_invalid_or_invented_decisions(change, error):
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


def test_v4_rejects_missing_pause_direction():
    compiler = _compiled()
    compiler.beat_tags[0].inserts[1].pause_direction = None
    with pytest.raises(pv.VoicePlanError, match="explicit pause function"):
        pv._check_v4(compiler, _block(), _artistic())


def test_v3_coincident_human_behavior_and_explicit_pause_preserve_source_order():
    direction = _artistic()
    direction.beat_deliveries[0].delivery[0].vocal_intents = [
        pv.VocalIntent(at=0, behavior="inhale"),
        pv.VocalIntent(at=6, behavior="exhale"),
    ]
    compiler = _compiled()
    compiler.beat_tags[0].inserts.insert(1, pv.V4TagSnippet(
        at=6, tag="exhales", text="Hola, <exhales>mundo",
        source_kind="vocal_intent", source_item_index=0, source_at=6,
    ))
    pv._check_v3(direction, _block())
    pv._check_v4(compiler, _block(), direction)
    assert pv._v4_turns(compiler, _block(), direction)[0]["text"] == (
        "<breath>Hola, <exhales><short pause>mundo. Esa es la realidad."
    )


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


def test_silent_breath_class_never_creates_unrequested_audible_inhale():
    direction = _artistic()
    direction.beat_deliveries[0].delivery[1] = pv.Pause(
        pause="breath", pause_function="Silently re-gather before resuming"
    )
    compiler = _compiled()
    compiler.beat_tags[0].inserts[1].pause_direction = (
        "Silently re-gather before resuming"
    )
    pv._check_v4(compiler, _block(), direction)
    compiler.beat_tags[0].inserts[1].tag = "breath"
    with pytest.raises(pv.VoicePlanError, match="invented, duplicated or moved"):
        pv._check_v4(compiler, _block(), direction)


def test_empty_whitespace_prose_cannot_bypass_v3_v4_validations():
    direction = _artistic()
    direction.block_performance.register_ = "   "
    with pytest.raises(pv.VoicePlanError, match="empty prose"):
        pv._check_v3(direction, _block())
    direction = _artistic()
    direction.beat_deliveries[0].delivery[1].pause_function = " "
    with pytest.raises(pv.VoicePlanError, match="audible purpose"):
        pv._check_v3(direction, _block())
    compiler = _compiled()
    compiler.audio_profile = "   "
    with pytest.raises(pv.VoicePlanError, match="substantive"):
        pv._check_v4(compiler, _block(), _artistic())
    compiler = _compiled()
    compiler.beat_tags[0].inserts[1].pause_direction = " "
    with pytest.raises(pv.VoicePlanError, match="explicit pause function"):
        pv._check_v4(compiler, _block(), _artistic())


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


def test_sparse_tag_excerpt_rejects_extra_words_or_wrong_position():
    compiler = _compiled()
    compiler.beat_tags[0].inserts[1].text = "Hola, <short pause>mundo. Esa"
    with pytest.raises(pv.VoicePlanError, match="exact adjacent words"):
        pv._check_v4(compiler, _block(), _artistic())
    compiler = _compiled()
    compiler.beat_tags[0].inserts[1].at = 5
    with pytest.raises(pv.VoicePlanError, match="invented, duplicated or moved"):
        pv._check_v4(compiler, _block(), _artistic())


def test_repeated_words_are_placed_by_original_offset_not_snippet_search():
    beat = {"beat_id": "R", "text": "Hola, como fue todo"}
    block = {"block_id": 12, "beats": [beat]}
    direction = pv.V3(
        pipeline_stage="V3", block_id=12,
        block_performance={"register": "Same narrator", "pace_character": "Even",
                           "intensity_arc": "Gentle rise"},
        beat_deliveries=[{"beat_id": "R", "delivery": [
            {"speak": beat["text"], "delivery_quality": "Natural",
             "ending_behavior": "resolve", "stress": [],
             "vocal_intents": [{"at": 5, "behavior": "inhale"}]}
        ]}],
    )
    compiler = pv.V4(
        pipeline_stage="V4", block_id=12, audio_profile="Same narrator",
        directors_notes=["Even delivery, gentle rise"],
        spoken_directions=[
            {"beat_id": "R", "source_item_index": 0, "locator": "todo",
             "instruction": "Natural, resolve naturally",
             "ending_behavior": "resolve", "stress": []},
        ],
        beat_tags=[{"beat_id": "R", "text": "", "inserts": [
            {"at": 5, "tag": "breath", "text": "Hola,<breath> como",
             "source_kind": "vocal_intent", "source_item_index": 0,
             "source_at": 5}
        ]}],
    )
    pv._check_v3(direction, block)
    pv._check_v4(compiler, block, direction)
    assert pv._v4_turns(compiler, block, direction)[0]["text"] == (
        "Hola,<breath> como fue todo"
    )


def test_identical_tag_excerpts_use_distinct_source_offsets():
    """Repeated words can produce identical excerpts: never search by text."""
    text = "como como como"
    block = {"block_id": 9, "beats": [{"beat_id": "R", "text": text}]}
    direction = pv.V3(
        pipeline_stage="V3", block_id=9,
        block_performance={"register": "Same narrator", "pace_character": "Natural",
                           "intensity_arc": "Unchanged"},
        beat_deliveries=[{"beat_id": "R", "delivery": [
            {"speak": text, "delivery_quality": "Conversational",
             "ending_behavior": "resolve", "stress": [],
             "vocal_intents": [
                 {"at": 5, "behavior": "inhale"},
                 {"at": 10, "behavior": "inhale"},
             ]}
        ]}],
    )
    compiler = pv.V4(
        pipeline_stage="V4", block_id=9, audio_profile="Same narrator",
        directors_notes=["Natural delivery"],
        spoken_directions=[
            {"beat_id": "R", "source_item_index": 0, "locator": text,
             "instruction": "Conversational; resolve naturally",
             "ending_behavior": "resolve", "stress": []},
        ],
        beat_tags=[{"beat_id": "R", "text": "", "inserts": [
            {"at": at, "tag": "breath", "text": "como <breath>como",
             "source_kind": "vocal_intent", "source_item_index": 0,
             "source_at": at}
            for at in (5, 10)
        ]}],
    )
    pv._check_v3(direction, block)
    pv._check_v4(compiler, block, direction)
    assert compiler.beat_tags[0].inserts[0].text == (
        compiler.beat_tags[0].inserts[1].text
    )
    assert pv._v4_turns(compiler, block, direction) == [{
        "text": "como <breath>como <breath>como",
        "style": pv._v4_style(compiler, direction),
    }]
    compiler.beat_tags[0].inserts[1].at = 5
    with pytest.raises(pv.VoicePlanError, match="duplicated or moved"):
        pv._check_v4(compiler, block, direction)


def test_v3_pause_function_rejects_inline_gemini_token():
    direction = _artistic()
    direction.beat_deliveries[0].delivery[1].pause_function = (
        "Let greeting register <short pause>"
    )
    with pytest.raises(pv.VoicePlanError, match="V3 inline-tag leakage"):
        pv._check_v3(direction, _block())


def test_v4_refuses_to_compile_or_render_contaminated_v3_prose():
    direction = _artistic()
    direction.block_performance.register_ = "Grounded <sigh> storyteller"
    with pytest.raises(pv.VoicePlanError, match="V3 inline-tag leakage"):
        pv._check_v4(_compiled(), _block(), direction)
    with pytest.raises(pv.VoicePlanError, match="V3 inline-tag leakage"):
        pv._v4_turns(_compiled(), _block(), direction)


def test_v4_can_omit_all_inline_tags_without_losing_authored_silence():
    block, direction, compiler = _block(), _artistic(), _compiled()
    compiler.beat_tags[0].inserts.clear()
    pv._check_v4(compiler, block, direction)
    turns = pv._v4_turns(compiler, block, direction)
    assert len(turns) == 1
    assert turns[0]["text"] == "Hola, mundo. Esa es la realidad."
    assert "Let the greeting register" in turns[0]["style"]
    assert "pause class: beat" in turns[0]["style"]


def test_v4_can_choose_only_one_of_multiple_authorized_events():
    compiler = _compiled()
    compiler.beat_tags[0].inserts = compiler.beat_tags[0].inserts[:1]
    pv._check_v4(compiler, _block(), _artistic())
    turns = pv._v4_turns(compiler, _block(), _artistic())
    assert turns[0]["text"] == "<breath>Hola, mundo. Esa es la realidad."
    assert "Let the greeting register" in turns[0]["style"]
