"""Offline regression tests for persistent per-avatar AI33 voice selection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_video_factory.providers.avatar_voice import ensure_avatar_voice
from ai_video_factory.providers.ai33_speech import AI33SpeechClient, voice_id_of
from scripts.pipeline import run_b_pipeline as runner

DEFAULT = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"
OTHER = "fishaudio_1234567890abcdef"


def _avatar(tmp_path: Path) -> Path:
    folder = tmp_path / "avatar"
    folder.mkdir()
    avatar = folder / "monje.png"
    avatar.write_bytes(b"PNG bytes are checked by the CLI, not this metadata helper")
    return avatar


def test_interactive_voice_id_is_saved_and_never_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    avatar = _avatar(tmp_path)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return OTHER

    monkeypatch.setattr("builtins.input", answer)
    voice = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, interactive=True,
    )
    assert voice.voice_id == OTHER and voice.mode == "voice_id"
    path = avatar.parent / ".voice" / avatar.name / "voice.json"
    assert json.loads(path.read_text(encoding="utf-8"))["voice_id"] == OTHER
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("The stored voice must not prompt again"),
    )
    assert ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, interactive=True,
    ) == voice
    assert len(prompts) == 1
    with pytest.raises(SystemExit, match="ya tiene una voz"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, input_voice_id=DEFAULT,
        )


def test_cancel_persists_default_without_prompts_or_paid_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    avatar = _avatar(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    first = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, interactive=True,
    )
    assert first.mode == "default" and first.voice_id == DEFAULT
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("Already configured"))
    second = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, interactive=True, api_key=None,
    )
    assert second == first


def test_audio_must_be_adjacent_and_rejects_traversal_or_links(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path)
    external = tmp_path / "external.wav"
    external.write_bytes(b"audio")
    with pytest.raises(ValueError, match="solo el nombre"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, input_audio_filename="../external.wav",
        )
    with pytest.raises(ValueError, match="no existe"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, input_audio_filename="missing.wav",
        )
    link = avatar.parent / "linked.wav"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("This platform does not permit symlink creation")
    with pytest.raises(ValueError, match="no existe"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, input_audio_filename=link.name,
        )


def test_clone_selected_offline_is_submitted_once_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    avatar = _avatar(tmp_path)
    reference = avatar.parent / "monje.wav"
    reference.write_bytes(b"example-audio")
    pending = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, input_audio_filename=reference.name, activate=False,
    )
    assert pending.mode == "clone" and pending.voice_id is None
    calls: list[object] = []

    class FakeClient:
        def clone_voice(self, *, audio_path: Path, name: str) -> dict:
            calls.append(("clone", audio_path, name))
            return {"task_id": "clone-task-id"}

        def poll(self, task_id: str) -> dict:
            calls.append(("poll", task_id))
            return {"status": "completed", "data": {"voice_id": OTHER}}

    voice = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, client=FakeClient(),
    )
    assert voice.voice_id == OTHER
    assert voice.audio_sha256 == hashlib.sha256(reference.read_bytes()).hexdigest()
    assert calls == [("clone", reference, avatar.stem), ("poll", "clone-task-id")]
    assert ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, client=FakeClient(),
    ) == voice
    assert len(calls) == 2
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("Clone must not reprompt"))


def test_ambiguous_paid_clone_is_not_resubmitted(
    tmp_path: Path,
) -> None:
    avatar = _avatar(tmp_path)
    reference = avatar.parent / "sample.mp3"
    reference.write_bytes(b"reference")
    submits: list[str] = []

    class FailingClient:
        def clone_voice(self, **_kwargs: object) -> dict:
            submits.append("POST")
            raise TimeoutError("ambiguous paid request")

    with pytest.raises(SystemExit, match="No se puede confirmar"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, input_audio_filename=reference.name,
            client=FailingClient(),
        )
    with pytest.raises(SystemExit, match="pudo haber sido cobrada"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, client=FailingClient(),
        )
    assert submits == ["POST"]


def test_pending_clone_reuses_task_id_without_another_submission(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path)
    reference = avatar.parent / "sample.wav"
    reference.write_bytes(b"reference")
    calls = []

    class FakeClient:
        def clone_voice(self, **_kwargs: object) -> dict:
            calls.append("submit")
            return {"task_id": "pending-id"}

        def poll(self, _task_id: str) -> dict:
            calls.append("poll")
            if calls.count("poll") == 1:
                raise TimeoutError("pending")
            return {"status": "completed", "voice_id": OTHER}

    client = FakeClient()
    with pytest.raises(SystemExit, match="sigue pendiente"):
        ensure_avatar_voice(
            avatar_file=avatar, avatars_dir=avatar.parent,
            default_voice_id=DEFAULT, input_audio_filename=reference.name, client=client,
        )
    ready = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, client=client,
    )
    assert ready.voice_id == OTHER
    assert calls == ["submit", "poll", "poll"]


def test_run_snapshot_selects_voice_even_if_global_default_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.settings, "ai33_fish_voice_id", DEFAULT)
    assert runner._run_voice_id(tmp_path) == DEFAULT
    (tmp_path / "avatar_voice.json").write_text(
        json.dumps({"mode": "voice_id", "voice_id": OTHER}), encoding="utf-8",
    )
    assert runner._run_voice_id(tmp_path) == OTHER
    monkeypatch.setattr(runner.settings, "ai33_fish_voice_id", "fishaudio_newdefault")
    assert runner._run_voice_id(tmp_path) == OTHER


def test_ai33_clone_multipart_and_voice_id_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.providers import ai33_speech

    reference = tmp_path / "sample.wav"
    reference.write_bytes(b"RIFFwav-content")
    seen = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self, *_args: object) -> bytes:
            return b'{"success": true, "data": {"voice_id": "clone_abcdef"}}'

    def open_url(request: object, *, timeout: int) -> Reply:
        seen.append((request, timeout))
        return Reply()

    monkeypatch.setattr(ai33_speech, "urlopen", open_url)
    result = AI33SpeechClient("fake-key").clone_voice(
        audio_path=reference, name="monje",
    )
    assert voice_id_of(result) == "clone_abcdef"
    assert voice_id_of({"data": {"task_id": "only-task"}}) is None
    request, timeout = seen[0]
    assert timeout == 120
    assert request.full_url.endswith("/v3/text-to-speech/voice-clone")
    assert b"RIFFwav-content" in request.data
    assert b'name="audio"' in request.data
    assert b'name="name"' in request.data
    assert request.get_header("Xi-api-key") == "fake-key"
