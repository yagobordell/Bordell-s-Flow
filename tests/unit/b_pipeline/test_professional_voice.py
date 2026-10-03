"""Offline contracts for professional V1/V2/V3 and Gemini 3.8 block TTS."""
import asyncio
import hashlib
import io
import json
import wave
from types import SimpleNamespace

import pytest

from ai_video_factory.bots import professional_voice as pv
from ai_video_factory.providers import gemini_tts
from ai_video_factory.providers.professional_avatar_voice import ensure_professional_avatar_voice


def _block():
    return {
        "block_id": 1, "type": "intro", "emotional_entry": "curiosity",
        "emotional_exit": "recognition",
        "span": {"first_words": "Hola", "last_words": "realidad"},
        "beats": [
            {"beat_id": "1A", "text": "Hola, mundo. ", "beat_type": "orientation",
             "visual_type": "avatar", "prompt": None, "video_prompt": None,
             "reference": False, "audio": True},
            {"beat_id": "1B", "text": "Esa es la realidad.", "beat_type": "revelation",
             "visual_type": "media_image", "prompt": "A door opening",
             "video_prompt": None, "reference": False, "audio": False},
        ],
    }


def _v1():
    return pv.V1(
        pipeline_stage="V1", curve="Quiet opening leads to consequential recognition.",
        block_rhythms=[{"block_id": 1, "phase": "Opens the central contradiction.",
                        "viewer_trajectory": "From curiosity to recognition.",
                        "tempo": "measured", "pressure": "moderate",
                        "contrast": "Establishes room for the coming explanation."}],
        baseline_reconsiderations=[],
    )


def _v2():
    return pv.V2(pipeline_stage="V2", block_id=1, beat_intents=[
        {"beat_id": beat, "function": f"Moves the argument through {beat}",
         "understanding_delta": "Recognises what the statement establishes.",
         "viewer_trajectory": "Shifts attention to its implication.",
         "tempo_modulation": "at", "pressure_modulation": "at"}
        for beat in ("1A", "1B")
    ], landing_beat_id="1B", baseline_misfit=[])


def _v3():
    return pv.V3(pipeline_stage="V3", block_id=1, beat_deliveries=[
        {"beat_id": "1A", "segments": [
            {"speak": "Hola, mundo. ", "style": "Measured, conversational",
             "ending_behavior": "carry",
             "events": [{"at": 6, "tag": "breath"}]},
        ]},
        {"beat_id": "1B", "segments": [
            {"speak": "Esa es la realidad.", "style": "Warm, deliberate",
             "ending_behavior": "land", "events": []},
        ]},
    ])


def _wav(rate=24000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\x00" * (rate // 10) * 2)
    return stream.getvalue()


def test_v3_verbatim_and_human_events():
    block = _block()
    result = _v3()
    pv._check_v3(result, block)
    assert pv._tagged(result.beat_deliveries[0].segments[0]) == "Hola, <breath>mundo. "
    result.beat_deliveries[0].segments[0].speak = "Hola mundo."
    with pytest.raises(pv.VoicePlanError, match="verbatim"):
        pv._check_v3(result, block)


def test_v3_rejects_split_word_and_extra_events():
    direction = _v3()
    direction.beat_deliveries[0].segments[0].events = [
        pv.VocalEvent(at=2, tag="sigh"),
    ]
    with pytest.raises(pv.VoicePlanError, match="splits a word"):
        pv._check_v3(direction, _block())
    direction = _v3()
    direction.beat_deliveries[0].segments[0].events = [
        pv.VocalEvent(at=0, tag="sigh"), pv.VocalEvent(at=6, tag="breath"),
        pv.VocalEvent(at=12, tag="long pause"),
    ]
    with pytest.raises(pv.VoicePlanError, match="too many events"):
        pv._check_v3(direction, _block())


def test_v2_beat_identity_and_modulation_edges():
    block = _block()
    rhythm = _v1().block_rhythms[0]
    pv._check_v2(_v2(), block, rhythm)
    bad = _v2()
    bad.beat_intents[0].beat_id = "1Z"
    with pytest.raises(pv.VoicePlanError, match="beat_ids"):
        pv._check_v2(bad, block, rhythm)
    rhythm.tempo = "held"
    bad = _v2()
    bad.beat_intents[0].tempo_modulation = "below"
    with pytest.raises(pv.VoicePlanError, match="scale edge"):
        pv._check_v2(bad, block, rhythm)


def test_v1_reconsideration_contract():
    pv._check_v1(_v1(), [_block()], [])
    contested = [{"block_id": 1, "axis": "pressure",
                  "contested_value": "moderate", "reading": "The scenes do not release"}]
    with pytest.raises(pv.VoicePlanError, match="reconsiderations"):
        pv._check_v1(_v1(), [_block()], contested)


def test_gemini_part_styles_not_spoken_and_only_allowlisted_tags():
    direction = _v3()
    turns = [{"text": pv._tagged(segment), "style": segment.style}
             for beat in direction.beat_deliveries for segment in beat.segments]
    payload = gemini_tts._build_directed_payload(turns=turns, voice="Algenib")
    parts = payload["contents"][0]["parts"]
    assert parts[0]["text"] == "Hola, <breath>mundo. "
    assert parts[0]["speech_metadata"]["style"].startswith("Measured, conversational")
    assert "verbatim transcript" in parts[0]["speech_metadata"]["style"]
    assert parts[1]["speech_metadata"]["style"].startswith("Warm, deliberate")
    assert "Measured" not in parts[0]["text"]


def test_professional_voice_is_separate_and_persistent(tmp_path):
    avatar = tmp_path / "Julia" / "Julia.png"
    avatar.parent.mkdir()
    avatar.write_bytes(b"not relevant to this selection test")
    first = ensure_professional_avatar_voice(
        avatar_file=avatar, input_voice_id="Kore", interactive=False)
    second = ensure_professional_avatar_voice(avatar_file=avatar, interactive=False)
    assert first.voice_id == second.voice_id == "Kore"
    assert json.loads((avatar.parent / "meta" / "gemini_voice.json").read_text())[
        "provider"] == "gemini"
    with pytest.raises(ValueError, match="different"):
        ensure_professional_avatar_voice(avatar_file=avatar, input_voice_id="Puck")


class FakeProvider:
    def __init__(self, invalid_v3=False):
        self.invalid_v3 = invalid_v3
        self.calls = []

    async def generate_structured_with_response(self, *, output_type, **kwargs):
        self.calls.append(output_type.__name__)
        if output_type is pv.V1:
            result = _v1()
        elif output_type is pv.V2:
            result = _v2()
        else:
            result = _v3()
            if self.invalid_v3 and self.calls.count("V3") == 1:
                result.beat_deliveries[0].segments[0].speak = "incorrect"
        return result, SimpleNamespace(id=str(len(self.calls)), usage=None, model="mock")


def test_plan_validation_retries_only_v3_and_persists_outputs(tmp_path):
    provider = FakeProvider(invalid_v3=True)
    result = asyncio.run(pv.plan_professional_voice(
        core={"central_question": "Why?", "final_answer": "Because."},
        blocks=[_block()], output=tmp_path, provider=provider, model="test",
    ))
    assert [type(item).__name__ for item in result] == ["V1", "list", "list"]
    assert provider.calls == ["V1", "V2", "V3", "V3"]
    assert (tmp_path / "voice_direction/V3/block_1/rejected_attempt_1.json").is_file()
    # The second invocation reuses validated V1/V2/V3 files without paid text calls.
    asyncio.run(pv.plan_professional_voice(
        core={"central_question": "Why?", "final_answer": "Because."},
        blocks=[_block()], output=tmp_path, provider=provider, model="test",
    ))
    assert provider.calls == ["V1", "V2", "V3", "V3"]


def test_professional_audio_paid_block_is_reused(tmp_path, monkeypatch):
    """A completed paid block is not re-posted while the state is verifiably intact."""
    revision = {"value": 0}

    async def plan(**_):
        rhythm = _v1()
        rhythm.curve += str(revision["value"])
        return rhythm, [_v2()], [_v3()]

    async def transcribe(item, *, output, run_dir, stt_client):
        folder = run_dir / "blocks" / "block_1"
        path = folder / "stt.json"
        path.write_text(json.dumps({"model": stt_client.model,
                                    "source_audio_sha256": item["sha256"],
                                    "words": [{"word": "Hola", "start": 0, "end": .05}]}))
        return {**item, "stt": {"file": path.relative_to(output).as_posix(),
                               "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                               "model": stt_client.model, "word_count": 1}}

    def join(inputs, dest):
        assert inputs[0].is_file()
        dest.write_bytes(_wav(48000))
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        return {"sha256": digest, "format": "wav", "bytes": dest.stat().st_size,
                "duration_seconds": 0.1, "block_start_seconds": [0.0],
                "block_end_seconds": [0.1], "block_durations_seconds": [0.1]}

    class Speech:
        calls = 0
        async def generate_directed_speech(self, **kwargs):
            self.calls += 1
            assert kwargs["turns"][0]["text"] == "Hola, <breath>mundo. "
            return SimpleNamespace(content=_wav())

    monkeypatch.setattr(pv, "plan_professional_voice", plan)
    monkeypatch.setattr(pv, "_transcribe_block", transcribe)
    monkeypatch.setattr(pv, "join_audio_blocks", join)
    speech = Speech()
    args = dict(core={"central_question": "Why?", "final_answer": "Because."},
                blocks=[_block()], output=tmp_path, provider=object(),
                model="test", gemini_api_key="test", voice_id="Algenib",
                source_sha256="a" * 64, speech_provider=speech,
                stt_client=SimpleNamespace(model="whisper-1"))
    first = asyncio.run(pv.generate_professional_audio(**args))
    second = asyncio.run(pv.generate_professional_audio(**args))
    assert first == second
    assert speech.calls == 1
    assert second["stt_complete"] and second["provider"] == "gemini"
    # A new B2-derived global curve changes the run fingerprint while
    # unchanged individual text/style still reuses the paid Gemini WAV.
    revision["value"] = 1
    third = asyncio.run(pv.generate_professional_audio(**args))
    assert third["run_id"] != first["run_id"]
    assert speech.calls == 1
    assert third["blocks"][0]["sha256"] == first["blocks"][0]["sha256"]
