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
    return pv.V3(
        pipeline_stage="V3", block_id=1,
        block_performance={
            "register": "Consistent intimate documentary narration",
            "pace_character": "Measured, then deliberate",
            "intensity_arc": "Curiosity gives way to recognition",
        },
        beat_deliveries=[
            {"beat_id": "1A", "delivery": [
                {"speak": "Hola, mundo. ", "delivery_quality": "Measured, conversational",
                 "ending_behavior": "carry", "stress": ["mundo"],
                 "vocal_intents": [{"at": 6, "behavior": "inhale"}]},
            ]},
            {"beat_id": "1B", "delivery": [
                {"speak": "Esa es la realidad.", "delivery_quality": "Warm, deliberate",
                 "ending_behavior": "land", "stress": [], "vocal_intents": []},
            ]},
        ],
    )


def _v4():
    return pv.V4(
        pipeline_stage="V4", block_id=1,
        audio_profile="Consistent intimate documentary narrator",
        directors_notes=["Measured beginning, then deliberate recognition."],
        spoken_directions=[
            {"beat_id": "1A", "source_item_index": 0, "locator": "mundo",
             "instruction": "Measured conversational delivery; emphasize mundo; keep open",
             "ending_behavior": "carry", "stress": ["mundo"]},
            {"beat_id": "1B", "source_item_index": 0, "locator": "realidad",
             "instruction": "Warm deliberate delivery; land decisively",
             "ending_behavior": "land", "stress": []},
        ],
        beat_tags=[
            {"beat_id": "1A", "text": "", "inserts": [
                {"at": 6, "tag": "breath", "text": "Hola, <breath>mundo",
                 "source_kind": "vocal_intent", "source_item_index": 0, "source_at": 6},
            ]},
            {"beat_id": "1B", "text": "", "inserts": []},
        ],
    )


def _wav(rate=24000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\x00" * (rate // 10) * 2)
    return stream.getvalue()


def test_v3_artistic_contract_and_v4_source_bound_tags():
    block = _block()
    result = _v3()
    pv._check_v3(result, block)
    compiler = _v4()
    pv._check_v4(compiler, block, result)
    assert pv._tagged_v4(block["beats"][0]["text"], compiler.beat_tags[0]) == (
        "Hola, <breath>mundo. "
    )
    result.beat_deliveries[0].delivery[0].speak = "Hola mundo."
    with pytest.raises(pv.VoicePlanError, match="verbatim"):
        pv._check_v3(result, block)


def test_v3_rejects_split_word_and_excess_humanization():
    direction = _v3()
    direction.beat_deliveries[0].delivery[0].vocal_intents = [
        pv.VocalIntent(at=2, behavior="sigh"),
    ]
    with pytest.raises(pv.VoicePlanError, match="splits a word"):
        pv._check_v3(direction, _block())
    direction = _v3()
    direction.beat_deliveries[0].delivery[0].vocal_intents = [
        pv.VocalIntent(at=0, behavior="sigh"),
        pv.VocalIntent(at=6, behavior="inhale"),
        pv.VocalIntent(at=12, behavior="exhale"),
    ]
    with pytest.raises(pv.VoicePlanError, match="excessive vocal intents"):
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


def test_gemini_one_style_and_one_transcript_per_complete_group():
    direction = _v4()
    turns = pv._v4_turns(direction, _block(), _v3())
    assert len(turns) == 1
    payload = gemini_tts._build_directed_payload(turns=turns, voice="Algenib")
    parts = payload["contents"][0]["parts"]
    assert len(parts) == 1
    assert parts[0]["text"] == "Hola, <breath>mundo. Esa es la realidad."
    style = parts[0]["speech_metadata"]["style"]
    assert "Measured conversational" in style
    assert "Warm deliberate" in style
    assert "Consistent intimate" in style
    assert "verbatim transcript" in style
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
        elif output_type is pv.V3:
            result = _v3()
            if self.invalid_v3 and self.calls.count("V3") == 1:
                result.beat_deliveries[0].delivery[0].speak = "incorrect"
        elif output_type is pv.V4:
            result = _v4()
        else:
            raise AssertionError(output_type)
        return result, SimpleNamespace(id=str(len(self.calls)), usage=None, model="mock")


def test_plan_validation_retries_only_v3_and_persists_outputs(tmp_path):
    provider = FakeProvider(invalid_v3=True)
    result = asyncio.run(pv.plan_professional_voice(
        core={"central_question": "Why?", "final_answer": "Because."},
        blocks=[_block()], output=tmp_path, provider=provider, model="test",
    ))
    assert [type(item).__name__ for item in result] == ["V1", "list", "list", "list"]
    assert provider.calls == ["V1", "V2", "V3", "V3", "V4"]
    assert (tmp_path / "V1/input.json").is_file()
    assert (tmp_path / "V1/output.json").is_file()
    assert (tmp_path / "V2/block_1/input.json").is_file()
    assert (tmp_path / "V2/block_1/output.json").is_file()
    assert (tmp_path / "V3/block_1/input.json").is_file()
    assert (tmp_path / "V3/block_1/output.json").is_file()
    assert not list(tmp_path.rglob("api_call_*.json"))
    assert not list(tmp_path.rglob("rejected_attempt_*.json"))
    assert not list(tmp_path.rglob("input_attempt_*.json"))
    for stage in ("V1", "V2", "V3", "V4"):
        assert (tmp_path / stage / "merged_output.json").is_file()
    # The second invocation reuses validated V1/V2/V3 files without paid text calls.
    asyncio.run(pv.plan_professional_voice(
        core={"central_question": "Why?", "final_answer": "Because."},
        blocks=[_block()], output=tmp_path, provider=provider, model="test",
    ))
    assert provider.calls == ["V1", "V2", "V3", "V3", "V4"]


def test_professional_audio_paid_block_is_reused(tmp_path, monkeypatch):
    """A completed paid block is not re-posted while the state is verifiably intact."""
    revision = {"value": 0}

    async def plan(**_):
        rhythm = _v1()
        rhythm.curve += str(revision["value"])
        return rhythm, [_v2()], [_v3()], [_v4()]

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
            assert len(kwargs["turns"]) == 1
            assert kwargs["turns"][0]["text"] == (
                "Hola, <breath>mundo. Esa es la realidad."
            )
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
    # A new named Studio voice is never allowed to borrow an old voice's WAV.
    fourth = asyncio.run(pv.generate_professional_audio(
        **{**args, "voice_id": "Uncle Enzo 3"}
    ))
    assert fourth["run_id"] != third["run_id"]
    assert fourth["voice_id"] == "Uncle Enzo 3"
    assert speech.calls == 2


def test_failed_gemini_block_is_retryable_without_unknown_state(tmp_path, monkeypatch):
    async def plan(**_):
        return _v1(), [_v2()], [_v3()], [_v4()]

    async def transcribe(item, *, output, run_dir, stt_client):
        folder = run_dir / "blocks" / "block_1"
        path = folder / "stt.json"
        path.write_text(json.dumps({
            "model": stt_client.model,
            "source_audio_sha256": item["sha256"],
            "words": [{"word": "Hola", "start": 0, "end": .05}],
        }))
        return {
            **item,
            "stt": {
                "file": path.relative_to(output).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "model": stt_client.model,
                "word_count": 1,
            },
        }

    def join(inputs, dest):
        assert inputs[0].is_file()
        dest.write_bytes(_wav(48000))
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        return {
            "sha256": digest, "format": "wav", "bytes": dest.stat().st_size,
            "duration_seconds": 0.1, "block_start_seconds": [0.0],
            "block_end_seconds": [0.1], "block_durations_seconds": [0.1],
        }

    class Speech:
        calls = 0

        async def generate_directed_speech(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider temporarily unavailable")
            return SimpleNamespace(content=_wav())

    monkeypatch.setattr(pv, "plan_professional_voice", plan)
    monkeypatch.setattr(pv, "_transcribe_block", transcribe)
    monkeypatch.setattr(pv, "join_audio_blocks", join)
    speech = Speech()
    args = dict(
        core={"central_question": "Why?", "final_answer": "Because."},
        blocks=[_block()], output=tmp_path, provider=object(), model="test",
        gemini_api_key="test", voice_id="Algenib", source_sha256="a" * 64,
        speech_provider=speech, stt_client=SimpleNamespace(model="whisper-1"),
    )

    with pytest.raises(pv.VoicePlanError, match="1 incomplete blocks"):
        asyncio.run(pv.generate_professional_audio(**args))
    task_files = list(tmp_path.glob("audio/runs/*/blocks/block_1/task.json"))
    assert len(task_files) == 1
    failed = json.loads(task_files[0].read_text())
    assert failed["status"] == "tts_failed"

    result = asyncio.run(pv.generate_professional_audio(**args))
    assert result["status"] == "completed"
    assert speech.calls == 2


def test_runner_professional_waits_for_b2_and_never_starts_fish(tmp_path, monkeypatch):
    """End-to-end routing: B2 must finish before V/Gemini, with AI33 unset."""
    from argparse import Namespace

    from PIL import Image

    from ai_video_factory.bots.contracts import B11Output
    from scripts.pipeline import run_b_pipeline as runner

    script = tmp_path / "input" / "sample.txt"
    script.parent.mkdir()
    script.write_text("Hola, mundo. Esa es la realidad.", encoding="utf-8")
    avatar = tmp_path / "avatars" / "Julia.png"
    avatar.parent.mkdir()
    Image.new("RGB", (32, 24)).save(avatar, "PNG")
    args = Namespace(
        visual_style="profesional", script_file=None,
        scripts_dir=script.parent, script=script.name, list_scripts=False,
        avatars_dir=avatar.parent, avatar=avatar.name, list_avatars=False,
        output=tmp_path / "output", max_parallel_calls=1,
        from_stage="B1.1", skip_images=True, no_audio=False,
        images_only=False, regenerate_audio=False, resume_audio=False,
        voice_id="Kore",
    )
    b11 = B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Why?", "final_answer": "Because."},
        "blocks": [{
            "block_id": 1, "type": "intro", "emotional_entry": "calm",
            "emotional_exit": "clear",
            "span": {"first_words": "Hola,", "last_words": "realidad."},
        }],
        "error": None,
    })
    steps = []

    async def fake_b(_source, **kwargs):
        kwargs["on_output"]("B1.1", None, b11.model_dump(mode="json"))
        steps.append("B2_done")
        return SimpleNamespace(
            b11=b11, b12=[SimpleNamespace(block_id=1)],
            b2=[SimpleNamespace(blocks=[
                SimpleNamespace(model_dump=lambda **_: _block())
            ])],
            visual_plan=lambda: {"visual_style": "profesional", "blocks": []},
        )

    async def fake_professional(**kwargs):
        steps.append("gemini_after_B2")
        assert kwargs["voice_id"] == "Kore"
        assert kwargs["blocks"][0]["beats"][0]["text"] == "Hola, mundo. "
        return {
            "status": "completed", "provider": "gemini",
            "voice_id": "Kore", "stt_complete": False,
            "file": "audio/mock.wav",
        }

    def forbidden(*_args, **_kwargs):
        pytest.fail("Professional must not start Fish or paid image generation")

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(
        runner, "ensure_avatar_qr",
        lambda *, avatar_file, **_: SimpleNamespace(
            url="https://example.test/book", png=avatar_file.read_bytes(),
            sha256=hashlib.sha256(avatar_file.read_bytes()).hexdigest(),
            generator="segno",
        ),
    )
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runner.settings, "gemini_api_key", "test-gemini-key")
    monkeypatch.setattr(runner.settings, "ai33_api_key", None)
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **_: object())
    monkeypatch.setattr(runner, "run_b_pipeline", fake_b)
    monkeypatch.setattr(runner, "generate_professional_audio", fake_professional)
    monkeypatch.setattr(runner, "_generate_audio", forbidden)
    monkeypatch.setattr(runner, "_generate_images", forbidden)

    asyncio.run(runner.main())
    assert steps == ["B2_done", "gemini_after_B2"]
    output = tmp_path / "output" / "sample"
    report = json.loads((output / "run_report.json").read_text())
    assert report["run"]["audio_generation"]["provider"] == "gemini"
    saved_voice = json.loads((output / "avatar_voice.json").read_text())
    assert saved_voice == {
        "mode": "voice_id", "status": "ready",
        "provider": "gemini", "voice_id": "Kore",
    }


def test_studio_voice_name_is_persisted_and_can_be_changed_explicitly(tmp_path) -> None:
    avatar = tmp_path / "Enzo" / "Enzo.png"
    avatar.parent.mkdir()
    avatar.write_bytes(b"test avatar")
    initial = ensure_professional_avatar_voice(
        avatar_file=avatar, input_voice_id="Algenib", interactive=False,
    )
    saved_snapshot = initial.snapshot()
    path = avatar.parent / "meta" / "gemini_voice.json"
    original = path.read_bytes()
    with pytest.raises(ValueError, match="different"):
        ensure_professional_avatar_voice(
            avatar_file=avatar, input_voice_id="Uncle Enzo 3", interactive=False,
        )
    assert path.read_bytes() == original
    replacement = ensure_professional_avatar_voice(
        avatar_file=avatar, input_voice_id="Uncle Enzo 3",
        allow_replace=True, interactive=False,
    )
    assert replacement.voice_id == "Uncle Enzo 3"
    assert json.loads(path.read_text()) == {
        "provider": "gemini", "voice_id": "Uncle Enzo 3",
    }
    assert saved_snapshot["voice_id"] == "Algenib"
    assert ensure_professional_avatar_voice(
        avatar_file=avatar, interactive=False,
    ).voice_id == "Uncle Enzo 3"
    with pytest.raises(ValueError, match="requires"):
        ensure_professional_avatar_voice(
            avatar_file=avatar, allow_replace=True, interactive=False,
        )
    assert not (path.parent / "gemini_voice.json.tmp").exists()


@pytest.mark.parametrize("voice", ["Uncle/Enzo", "Uncle\nEnzo", "a", "Bad  Name"])
def test_invalid_studio_voice_does_not_overwrite_config(tmp_path, voice) -> None:
    avatar = tmp_path / "Enzo" / "Enzo.png"
    avatar.parent.mkdir()
    avatar.write_bytes(b"avatar")
    ensure_professional_avatar_voice(
        avatar_file=avatar, input_voice_id="Algenib", interactive=False,
    )
    path = avatar.parent / "meta" / "gemini_voice.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="voice"):
        ensure_professional_avatar_voice(
            avatar_file=avatar, input_voice_id=voice, allow_replace=True,
            interactive=False,
        )
    assert path.read_bytes() == before


def test_cli_requires_explicit_custom_voice_replacement_for_new_runs(
    monkeypatch,
) -> None:
    import sys

    from scripts.pipeline import run_b_pipeline as runner

    monkeypatch.setattr(sys, "argv", [
        "run_b_pipeline.py", "--style", "profesional",
        "--voice-id", "Uncle Enzo 3", "--replace-professional-voice",
    ])
    args = runner.parse_args()
    assert args.voice_id == "Uncle Enzo 3"
    assert args.replace_professional_voice is True

    monkeypatch.setattr(sys, "argv", [
        "run_b_pipeline.py", "--replace-professional-voice",
    ])
    with pytest.raises(SystemExit):
        runner.parse_args()

    monkeypatch.setattr(sys, "argv", [
        "run_b_pipeline.py", "--voice-id", "Uncle Enzo 3",
        "--replace-professional-voice", "--from", "B2",
    ])
    with pytest.raises(SystemExit):
        runner.parse_args()



def test_professional_voice_stage_wall_times_are_separate_from_parallel_call_time(
    tmp_path,
) -> None:
    provider = FakeProvider()
    stage_times = []
    call_times = []
    kwargs = {
        "core": {"central_question": "Why?", "final_answer": "Because."},
        "blocks": [_block()], "output": tmp_path, "provider": provider,
        "model": "gpt-6-luna",
        "on_timing": lambda stage, block, elapsed: call_times.append(
            (stage, block, elapsed)
        ),
        "on_stage_duration": lambda stage, elapsed: stage_times.append((stage, elapsed)),
    }
    asyncio.run(pv.plan_professional_voice(**kwargs))
    assert [stage for stage, _ in stage_times] == ["V1", "V2", "V3", "V4"]
    assert [stage for stage, _, _ in call_times] == ["V1", "V2", "V3", "V4"]
    assert all(seconds >= 0 for _, seconds in stage_times)
    assert all(seconds >= 0 for _, _, seconds in call_times)
    # Existing checkpoints never trigger paid V calls a second time.
    asyncio.run(pv.plan_professional_voice(**kwargs))
    assert len(provider.calls) == 4
    assert len(call_times) == 4
    assert len(stage_times) == 8
    assert not list(tmp_path.rglob("api_call_*.json"))


def test_legacy_professional_voice_checkpoints_are_rejected(
    tmp_path,
) -> None:
    legacy = tmp_path / "voice_direction"
    provider = FakeProvider()
    kwargs = {
        "core": {"central_question": "Why?", "final_answer": "Because."},
        "blocks": [_block()], "output": tmp_path, "provider": provider,
        "model": "gpt-6-luna",
    }
    asyncio.run(pv.plan_professional_voice(**kwargs))
    # Move only the completed V checkpoints to the old layout as if a prior
    # version produced them; no model calls should be needed afterwards.
    legacy.mkdir()
    for stage in ("V1", "V2", "V3", "V4"):
        (tmp_path / stage).rename(legacy / stage)
    with pytest.raises(pv.VoicePlanError, match="Legacy voice checkpoints"):
        asyncio.run(pv.plan_professional_voice(**kwargs))


def test_v_retries_keep_only_stable_input_and_output_files(tmp_path) -> None:
    class WithUsage(FakeProvider):
        async def generate_structured_with_response(self, *, output_type, **kwargs):
            result, raw = await super().generate_structured_with_response(
                output_type=output_type, **kwargs,
            )
            raw.model = "gpt-6-luna"
            raw.service_tier = "default"
            raw.usage = SimpleNamespace(
                input_tokens=100, output_tokens=10,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=0, cache_write_tokens=0,
                ),
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            )
            return result, raw

    provider = WithUsage(invalid_v3=True)
    asyncio.run(pv.plan_professional_voice(
        core={"central_question": "Why?", "final_answer": "Because."},
        blocks=[_block()], output=tmp_path, provider=provider, model="gpt-6-luna",
    ))
    assert (tmp_path / "V3/block_1/input.json").is_file()
    assert (tmp_path / "V3/block_1/output.json").is_file()
    assert not list(tmp_path.rglob("api_call_*.json"))
    assert not list(tmp_path.rglob("rejected_attempt_*.json"))
    assert not list(tmp_path.rglob("input_attempt_*.json"))


@pytest.mark.parametrize(("field", "token"), [
    ("register", "<breath>"),
    ("pace_character", "<short pause>"),
    ("intensity_arc", "<sigh>"),
    ("delivery_quality", "<chuckle>"),
])
def test_v3_refuses_inline_compiler_tokens_in_every_artistic_field(field, token):
    source, direction = _block(), _v3()
    if field == "delivery_quality":
        direction.beat_deliveries[0].delivery[0].delivery_quality += " " + token
    elif field == "register":
        direction.block_performance.register_ += " " + token
    else:
        setattr(direction.block_performance, field,
                getattr(direction.block_performance, field) + " " + token)
    with pytest.raises(pv.VoicePlanError, match="V3 inline-tag leakage"):
        pv._check_v3(direction, source)


def test_v3_inline_code_in_copied_narration_gets_specific_validation_error():
    direction = _v3()
    direction.beat_deliveries[0].delivery[0].speak = "Hola, <breath>mundo. "
    with pytest.raises(pv.VoicePlanError, match="V3 inline-tag leakage"):
        pv._check_v3(direction, _block())


def test_v3_inline_markup_receives_clear_local_retry_without_echoing_tokens(tmp_path):
    class Contaminated(FakeProvider):
        def __init__(self):
            super().__init__()
            self.feedback = []

        async def generate_structured_with_response(self, *, output_type, **kwargs):
            if output_type is pv.V3 and self.calls.count("V3"):
                self.feedback.append(json.loads(kwargs["input_text"])["validation_feedback"])
            result, raw = await super().generate_structured_with_response(
                output_type=output_type, **kwargs,
            )
            if output_type is pv.V3 and self.calls.count("V3") == 1:
                result.beat_deliveries[0].delivery[0].delivery_quality += " <breath>"
            return result, raw

    provider = Contaminated()
    v1, v2, v3, v4 = asyncio.run(pv.plan_professional_voice(
        core={"central_question": "Why?", "final_answer": "Because."},
        blocks=[_block()], output=tmp_path, provider=provider, model="test",
    ))
    assert provider.calls == ["V1", "V2", "V3", "V3", "V4"]
    assert len(provider.feedback) == 1
    assert "V3 inline-tag leakage" in provider.feedback[0]["error"]
    assert "<breath>" not in json.dumps(provider.feedback[0])
    assert "V4" in provider.feedback[0]["task"]
    assert "<" not in v3[0].beat_deliveries[0].delivery[0].delivery_quality
    assert pv._v4_turns(v4[0], _block(), v3[0])[0]["text"] == (
        "Hola, <breath>mundo. Esa es la realidad."
    )


def test_tagged_modern_v3_checkpoint_is_repaired_only_for_its_block(tmp_path):
    kwargs = {
        "core": {"central_question": "Why?", "final_answer": "Because."},
        "blocks": [_block()], "output": tmp_path, "model": "test",
    }
    provider = FakeProvider()
    asyncio.run(pv.plan_professional_voice(**kwargs, provider=provider))
    assert provider.calls == ["V1", "V2", "V3", "V4"]
    checkpoint = tmp_path / "V3" / "block_1" / "output.json"
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    data["beat_deliveries"][0]["delivery"][0]["delivery_quality"] += " <sigh>"
    checkpoint.write_text(json.dumps(data), encoding="utf-8")
    repair = FakeProvider()
    asyncio.run(pv.plan_professional_voice(**kwargs, provider=repair))
    assert repair.calls == ["V3"]
    assert json.loads(checkpoint.read_text())["beat_deliveries"][0]["delivery"][0][
        "delivery_quality"
    ] == "Measured, conversational"
    rejected = tmp_path / "V3" / "block_1" / "rejected_v3_inline_tags.json"
    assert "<sigh>" in rejected.read_text(encoding="utf-8")
