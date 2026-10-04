"""Offline contracts for 1/2/3 casero voice sources over the shared OpenSpeaker API."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from ai_video_factory.bots import fish_audio_workflow as audio_workflow
from ai_video_factory.bots.contracts import B11Output
from ai_video_factory.bots.fish_audio_workflow import (
    FishAudioWorkflowError,
    generate_fish_audio,
    unfinished_audio_runs,
)
from ai_video_factory.providers.avatar_voice import ensure_avatar_voice
from ai_video_factory.workflows import casero_cta
from scripts.pipeline import run_b_pipeline as runner

DEFAULT = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"
SCRIPT = "Una introducción. Una conclusión."
B11 = B11Output.model_validate({
    "pipeline_stage": "B1.1",
    "narrative_core": {"central_question": "Pregunta", "final_answer": "Respuesta"},
    "blocks": [{
        "block_id": 1, "type": "intro",
        "emotional_entry": "neutral", "emotional_exit": "curious",
        "span": {"first_words": "Una introducción.", "last_words": "Una conclusión."},
    }],
    "error": None,
})


def _avatar(tmp_path: Path) -> Path:
    root = tmp_path / "avatar"
    root.mkdir()
    avatar = root / "avatar.png"
    avatar.write_bytes(b"avatar fixture")
    return avatar


@pytest.mark.parametrize(
    ("option", "provider", "voice_id"),
    [
        ("1", "fishaudio", "fishaudio_example_01"),
        ("2", "minimax", "minimax_example_01"),
        ("3", "elevenlabs", "elevenlabs_example_01"),
    ],
)
def test_casero_prompts_provider_before_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    option: str, provider: str, voice_id: str,
) -> None:
    avatar = _avatar(tmp_path)
    seen: list[str] = []

    def respond(prompt: str) -> str:
        seen.append(prompt)
        if "Selecciona 1, 2 o 3" in prompt:
            return option
        return voice_id

    monkeypatch.setattr("builtins.input", respond)
    selected = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, interactive=True, allow_sources=True,
    )
    assert selected.provider == provider
    assert selected.voice_id == voice_id
    assert selected.snapshot()["provider"] == provider
    assert "Selecciona 1, 2 o 3" in seen[0]
    assert "Voice ID" in seen[1]
    path = avatar.parent / ".voice" / avatar.name / "voice.json"
    assert json.loads(path.read_text(encoding="utf-8"))["provider"] == provider
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Prompted existing avatar"))
    assert ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, interactive=True, allow_sources=True,
    ) == selected
    if provider != "fishaudio":
        with pytest.raises(SystemExit, match="solo para Casero"):
            ensure_avatar_voice(
                avatar_file=avatar, avatars_dir=avatar.parent,
                default_voice_id=DEFAULT, interactive=False, allow_sources=False,
            )
    other = "elevenlabs" if provider != "elevenlabs" else "minimax"
    with pytest.raises(SystemExit, match="otro proveedor"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, interactive=False, allow_sources=True,
            input_provider=other,
        )


@pytest.mark.parametrize("provider", ["minimax", "elevenlabs"])
def test_new_non_fish_selection_requires_id_not_fish_clone(
    tmp_path: Path, provider: str,
) -> None:
    avatar = _avatar(tmp_path)
    with pytest.raises(SystemExit, match="indica --voice-id"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, allow_sources=True,
            input_provider=provider, interactive=False,
        )
    with pytest.raises(SystemExit, match="necesitan --voice-id"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, allow_sources=True,
            input_provider=provider, input_audio_filename="fake.wav", interactive=False,
        )


def test_legacy_avatar_defaults_to_fish_without_rewriting_old_record(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path)
    legacy = avatar.parent / ".voice" / avatar.name / "voice.json"
    legacy.parent.mkdir(parents=True)
    original = {
        "avatar_filename": avatar.name,
        "mode": "voice_id", "voice_id": DEFAULT, "status": "ready",
    }
    legacy.write_text(json.dumps(original), encoding="utf-8")
    got = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, allow_sources=True, interactive=True,
    )
    assert got.provider == "fishaudio"
    assert json.loads(legacy.read_text(encoding="utf-8")) == original
    assert runner._run_voice_provider(tmp_path) == "fishaudio"
    (tmp_path / "avatar_voice.json").write_text(
        json.dumps(got.snapshot()), encoding="utf-8",
    )
    assert runner._run_voice_provider(tmp_path) == "fishaudio"


class FakeSpeech:
    def __init__(self, *, unknown: bool = False):
        self.creates: list[dict] = []
        self.unknown = unknown

    def create(self, **kwargs):
        self.creates.append(kwargs)
        if self.unknown:
            raise TimeoutError("Unknown paid POST outcome")
        return {"task_id": "submitted-tts"}

    def poll(self, _task_id, **_kwargs):
        return {
            "status": "done", "credit_cost": 30,
            "metadata": {"audio_url": "https://example.test/audio.mp3"},
        }

    def download_audio(self, _url, directory):
        content = b"ID3" + b"\x00" * 16
        path = directory / "audio.mp3"
        path.write_bytes(content)
        return {
            "file": path.name, "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content), "format": "mp3",
        }


@pytest.mark.parametrize("provider", ["minimax", "elevenlabs"])
def test_other_voice_sources_never_call_director(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    speech = FakeSpeech()

    class ForbiddenDirector:
        async def generate_structured_with_response(self, **_kwargs):
            pytest.fail("Fish director must never be called for this voice source")

    def fake_join(_inputs, destination):
        destination.write_bytes(b"RIFF" + b"\x00" * 40)
        return {
            "file": destination.name,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "format": "wav", "bytes": destination.stat().st_size,
            "duration_seconds": 1.0, "block_start_seconds": [0.0],
            "block_durations_seconds": [1.0],
        }

    monkeypatch.setattr(audio_workflow, "join_audio_blocks", fake_join)
    artifact = asyncio.run(generate_fish_audio(
        SCRIPT, tmp_path, b11=B11, provider=ForbiddenDirector(),
        ai33_api_key=None, speech_client=speech,
        director_model="gpt-6-luna", voice_provider=provider,
        voice_id=provider + "_selected_01",
    ))
    assert len(speech.creates) == 1
    assert speech.creates[0]["text"] == SCRIPT
    assert artifact["voice_source"] == provider
    assert artifact["director_model"] is None
    block = tmp_path / "audio" / "runs" / artifact["run_id"] / "blocks" / "block_1"
    assert json.loads((block / "output.json").read_text(encoding="utf-8")) == {
        "plain_script_for_recording": SCRIPT,
    }
    assert "director_usage" not in json.loads((block / "task.json").read_text())


def test_resume_rejects_switching_voice_library_before_paid_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    speech = FakeSpeech(unknown=True)
    with pytest.raises(FishAudioWorkflowError, match="outcome unknown"):
        asyncio.run(generate_fish_audio(
            SCRIPT, tmp_path, b11=B11, provider=None,
            ai33_api_key=None, speech_client=speech,
            director_model="gpt-6-luna", voice_provider="elevenlabs",
            voice_id="elevenlabs_selected_01",
        ))
    assert len(unfinished_audio_runs(tmp_path)) == 1
    with pytest.raises(FishAudioWorkflowError, match="differs"):
        asyncio.run(generate_fish_audio(
            SCRIPT, tmp_path, b11=B11, provider=None,
            ai33_api_key=None, speech_client=speech,
            director_model="gpt-6-luna", voice_provider="minimax",
            voice_id="elevenlabs_selected_01", resume=True,
        ))
    assert len(speech.creates) == 1


def test_ebook_tts_refuses_reusing_other_voice_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        casero_cta, "AI33SpeechClient", lambda *_args: FakeSpeech(unknown=True),
    )
    monkeypatch.setattr(casero_cta.settings, "ai33_api_key", "fake")
    with pytest.raises(TimeoutError):
        casero_cta._tts(tmp_path, "Compra este libro", "shared_id_01", "elevenlabs")
    saved = json.loads((tmp_path / "tts_state.json").read_text())
    assert saved["voice_provider"] == "elevenlabs"
    with pytest.raises(casero_cta.CaseroCTAError, match="different"):
        casero_cta._tts(tmp_path, "Compra este libro", "shared_id_01", "minimax")
