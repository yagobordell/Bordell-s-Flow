"""Offline contracts for Fish director + OpenSpeaker TTS and isolated audio modes."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from ai_video_factory.bots.fish_audio import (
    FishAudioScript,
    FishAudioScriptError,
    fish_prompt_bytes,
    run_fish_director,
    validate_fish_script,
)
from ai_video_factory.bots.contracts import B11Output, B12Input, MaterializedBlock, NarrativeCore
from ai_video_factory.bots.fish_audio_workflow import (
    FishAudioWorkflowError,
    generate_fish_audio,
    unfinished_audio_runs,
)
from ai_video_factory.providers.ai33_speech import AI33SpeechClient, audio_url_of
from ai_video_factory.bots import fish_audio_workflow as fish_workflow
from scripts.pipeline import run_b_pipeline as runner

VOICE = "fishaudio_f8dfe9c83081432386f143e2fe9767ef"
SCRIPT = "  Hola,\r\n[existing] ¿Qué tal?  \n"
DIRECTED = "  [warm]Hola,\r\n[existing] [curious]¿Qué tal?  \n"
B11 = B11Output.model_validate({
    "pipeline_stage": "B1.1",
    "narrative_core": {"central_question": "¿Qué tal?", "final_answer": "Bien."},
    "blocks": [{
        "block_id": 1, "type": "intro", "emotional_entry": "calm",
        "emotional_exit": "curious",
        "span": {"first_words": "Hola,", "last_words": "¿Qué tal?"},
    }],
    "error": None,
})
INPUT = B12Input(
    narrative_core=NarrativeCore(central_question="¿Qué tal?", final_answer="Bien."),
    current_block_id=1,
    blocks=(MaterializedBlock(
        block_id=1, type="intro", emotional_entry="calm",
        emotional_exit="curious", text=SCRIPT,
    ),),
)


@pytest.fixture(autouse=True)
def stub_audio_join(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_join(_inputs, destination):
        destination.write_bytes(b"RIFF" + b"\x00" * 40)
        return {
            "file": destination.name, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "format": "wav", "bytes": destination.stat().st_size, "duration_seconds": 1.0,
        }
    monkeypatch.setattr(fish_workflow, "join_audio_blocks", fake_join)


class FakeDirector:
    def __init__(self, output: str = DIRECTED) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    async def generate_structured_with_response(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        return (
            FishAudioScript(plain_script_for_recording=self.output),
            SimpleNamespace(usage=SimpleNamespace(model_dump=lambda **_kw: {
                "input_tokens": 50, "output_tokens": 11,
            })),
        )


class FakeSpeech:
    def __init__(self, *, uncertain: bool = False) -> None:
        self.creates: list[dict[str, object]] = []
        self.polls: list[str] = []
        self.downloads: list[str] = []
        self.uncertain = uncertain

    def create(self, **kwargs):
        self.creates.append(kwargs)
        if self.uncertain:
            raise URLError("lost reply after paid TTS submit")
        return {"success": True, "data": {"task_id": "fish-task-1"}}

    def poll(self, task_id, **_kwargs):
        self.polls.append(task_id)
        return {
            "id": task_id,
            "status": "done",
            "credit_cost": 321,
            "metadata": {"audio_url": "https://audio.example.test/generated.mp3"},
        }

    def download_audio(self, url, directory):
        self.downloads.append(url)
        directory.mkdir(parents=True, exist_ok=True)
        payload = b"ID3" + b"\x04\x00" + b"\x00" * 40
        (directory / "audio.mp3").write_bytes(payload)
        return {
            "file": "audio.mp3",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "format": "mp3",
        }


def test_fish_director_prompt_is_exact_user_contract() -> None:
    prompt = fish_prompt_bytes().decode("utf-8")
    assert "current_block_id" in prompt
    assert "narrative_core" in prompt
    assert "emotional_exit" in prompt
    assert "original line-ending sequence" in prompt or "line endings" in prompt
    assert "Return the final JSON only." in prompt


def test_insertion_only_validator_preserves_crlf_bracketed_text_and_whitespace() -> None:
    tags = validate_fish_script(SCRIPT, DIRECTED)
    assert tags == ("[warm]", "[curious]")
    assert validate_fish_script(SCRIPT, SCRIPT) == ()


@pytest.mark.parametrize(
    ("changed", "error"),
    [
        ("  [warm]Hola,\r\n[existing] ¿Qué tal?  \nAdded", "changed"),
        ("  [warm]Hola,\r\n[existing] ¿Qué tal?  \n[soft]", "trail"),
        ("  H[soft]ola,\r\n[existing] ¿Qué tal?  \n", "split"),
    ],
)
def test_director_rejects_noninsertions(
    changed: str, error: str
) -> None:
    with pytest.raises(FishAudioScriptError, match=error):
        validate_fish_script(SCRIPT, changed)


def test_director_uses_same_json_input_as_b12() -> None:
    fake = FakeDirector()
    output, response = asyncio.run(
        run_fish_director(INPUT, provider=fake, model="gpt-6-luna")
    )
    assert fake.calls[0]["model"] == "gpt-6-luna"
    assert json.loads(fake.calls[0]["input_text"]) == INPUT.model_dump()
    assert fake.calls[0]["output_type"] is FishAudioScript
    assert output.plain_script_for_recording == DIRECTED
    assert response is not None


def test_director_refuses_source_rewrite_before_paid_tts(tmp_path: Path) -> None:
    speech = FakeSpeech()
    bad = FakeDirector("Texto alterado")
    with pytest.raises(FishAudioScriptError):
        asyncio.run(
            generate_fish_audio(
                SCRIPT,
                tmp_path,
                b11=B11,
                provider=bad,
                director_model="gpt-6-luna",
                ai33_api_key=None,
                speech_client=speech,
            )
        )
    assert not speech.creates
    states = sorted((tmp_path / "audio/runs").glob("*/task.json"))
    assert len(states) == 1
    assert json.loads(states[0].read_text(encoding="utf-8"))["status"] == "director_invalid"
    assert unfinished_audio_runs(tmp_path) == []
    corrected = asyncio.run(
        generate_fish_audio(
            SCRIPT,
            tmp_path,
                b11=B11,
            provider=FakeDirector(),
            director_model="gpt-6-luna",
            ai33_api_key=None,
            speech_client=speech,
        )
    )
    assert corrected["status"] == "completed"
    assert len(speech.creates) == 1


def test_speech_endpoints_payload_and_audio_url_extraction(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    client = AI33SpeechClient("test-key")
    calls = []
    monkeypatch.setattr(
        client,
        "request_json",
        lambda method, path, payload=None: (
            calls.append((method, path, payload))
            or {"task_id": "fish-task-1"}
        ),
    )
    result = client.create(text=DIRECTED, voice_id=VOICE)
    assert result["task_id"] == "fish-task-1"
    assert calls == [(
        "POST",
        "/v3/text-to-speech",
        {"text": DIRECTED, "voice_id": VOICE, "speed": 1.0},
    )]
    assert audio_url_of({
        "metadata": {"result_audio": {"audioUrl": "https://example.test/a.mp3"}}
    }) == "https://example.test/a.mp3"
    assert audio_url_of({"metadata": {"imageUrl": "https://example.test/x.png"}}) is None


def test_audio_generation_and_explicit_regeneration_do_not_repeat_bots(
    tmp_path: Path,
) -> None:
    director = FakeDirector()
    speech = FakeSpeech()
    first = asyncio.run(
        generate_fish_audio(
            SCRIPT,
            tmp_path,
                b11=B11,
            provider=director,
            director_model="gpt-6-luna",
            ai33_api_key=None,
            voice_id=VOICE,
            speech_client=speech,
        )
    )
    assert first["status"] == "completed"
    assert first["voice_id"] == VOICE
    assert first["credit_cost"] == 321
    assert len(director.calls) == len(speech.creates) == 1
    assert speech.creates[0]["text"] == DIRECTED
    assert (tmp_path / first["file"]).read_bytes().startswith(b"ID3")
    state = json.loads(
        (tmp_path / "audio/runs" / first["run_id"] / "task.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["source_sha256"] == hashlib.sha256(SCRIPT.encode()).hexdigest()
    assert state["status"] == "completed"
    assert first["blocks"][0]["task_id"] == "fish-task-1"
    assert json.loads(
        (tmp_path / "audio/runs" / first["run_id"] / "blocks/block_1/input.json").read_text(
            encoding="utf-8"
        )
    ) == INPUT.model_dump()
    assert json.loads(
        (tmp_path / "audio/runs" / first["run_id"] / "blocks/block_1/output.json").read_text(
            encoding="utf-8"
        )
    ) == {"plain_script_for_recording": DIRECTED}

    again = asyncio.run(
        generate_fish_audio(
            SCRIPT,
            tmp_path,
                b11=B11,
            provider=director,
            director_model="gpt-6-luna",
            ai33_api_key=None,
            speech_client=speech,
        )
    )
    assert again["run_id"] != first["run_id"]
    assert len(director.calls) == len(speech.creates) == 2
    assert (tmp_path / first["file"]).exists()
    latest = json.loads((tmp_path / "audio/latest.json").read_text(encoding="utf-8"))
    assert latest["run_id"] == again["run_id"]


def test_unknown_paid_tts_submission_is_never_resent_on_resume(tmp_path: Path) -> None:
    fake = FakeSpeech(uncertain=True)
    with pytest.raises(FishAudioWorkflowError, match="outcome unknown"):
        asyncio.run(
            generate_fish_audio(
                SCRIPT,
                tmp_path,
                b11=B11,
                provider=FakeDirector(),
                director_model="gpt-6-luna",
                ai33_api_key=None,
                speech_client=fake,
            )
        )
    assert len(fake.creates) == 1
    assert len(unfinished_audio_runs(tmp_path)) == 1
    with pytest.raises(FishAudioWorkflowError, match="unknown outcome"):
        asyncio.run(
            generate_fish_audio(
                SCRIPT,
                tmp_path,
                b11=B11,
                provider=None,
                director_model="gpt-6-luna",
                ai33_api_key=None,
                speech_client=fake,
                resume=True,
            )
        )
    assert len(fake.creates) == 1


def test_resume_known_task_polls_without_recalling_director_or_paid_post(
    tmp_path: Path,
) -> None:
    fake = FakeSpeech()
    pending = tmp_path / "audio/runs/old/task.json"
    pending.parent.mkdir(parents=True)
    (pending.parent / "blocks/block_1").mkdir(parents=True)
    (pending.parent / "blocks/block_1/input.json").write_text(
        json.dumps(INPUT.model_dump()), encoding="utf-8"
    )
    text = DIRECTED
    (pending.parent / "blocks/block_1/output.json").write_text(
        json.dumps({"plain_script_for_recording": text}), encoding="utf-8"
    )
    input_sha = hashlib.sha256(
        json.dumps([INPUT.model_dump()], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    pending.write_text(
        json.dumps({
            "run_id": "old",
            "source_sha256": hashlib.sha256(SCRIPT.encode()).hexdigest(),
            "inputs_sha256": input_sha,
            "director_model": "gpt-6-luna",
            "director_prompt_sha256": hashlib.sha256(fish_prompt_bytes()).hexdigest(),
            "voice_id": VOICE, "speed": 1.0, "status": "running",
        }),
        encoding="utf-8",
    )
    (pending.parent / "blocks/block_1/task.json").write_text(
        json.dumps({
            "block_id": 1, "source_sha256": hashlib.sha256(SCRIPT.encode()).hexdigest(),
            "director_model": "gpt-6-luna", "voice_id": VOICE, "speed": 1.0,
            "task_id": "existing", "status": "submitted",
        }), encoding="utf-8",
    )
    result = asyncio.run(
        generate_fish_audio(
            SCRIPT,
            tmp_path,
                b11=B11,
            provider=None,
            director_model="gpt-6-luna",
            ai33_api_key=None,
            speech_client=fake,
            resume=True,
        )
    )
    assert result["blocks"][0]["task_id"] == "existing"
    assert fake.creates == []
    assert fake.polls == ["existing"]
    assert unfinished_audio_runs(tmp_path) == []


@pytest.mark.parametrize(
    ("args", "attribute"),
    [
        (["--no-audio"], "no_audio"),
        (["--regenerate-audio"], "regenerate_audio"),
        (["--resume-audio"], "resume_audio"),
    ],
)
def test_audio_cli_flags(
    args: list[str], attribute: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["run_b_pipeline.py", "--script", "test2", *args])
    parsed = runner.parse_args()
    assert getattr(parsed, attribute) is True


def test_audio_regeneration_conflicts_with_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["run_b_pipeline.py", "--regenerate-audio", "--no-image"],
    )
    with pytest.raises(SystemExit):
        runner.parse_args()
