"""Offline coverage for per-avatar folders and idempotent legacy migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.providers.avatar_library import organize_avatar
from ai_video_factory.providers.avatar_qr import ensure_avatar_qr
from ai_video_factory.providers.avatar_voice import ensure_avatar_voice
from scripts.pipeline import run_b_pipeline as runner

DEFAULT = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"
CUSTOM = "fishaudio_1234567890abcdef"
URL = "https://ebooks.example/Jorge"


def _avatar(tmp_path: Path, *, name: str = "Jorge.png") -> Path:
    library = tmp_path / "avatar"
    library.mkdir()
    avatar = library / name
    Image.new("RGB", (3, 2), "white").save(avatar, format="PNG")
    return avatar


def test_first_use_migrates_png_qr_voice_and_arbitrary_reference_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = _avatar(tmp_path)
    library = avatar.parent
    old_bytes = avatar.read_bytes()
    (library / "muestra.wav").write_bytes(b"reference-audio")
    qr = ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=library, input_url=URL,
    )
    voice = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=library, default_voice_id=DEFAULT,
        input_audio_filename="muestra.wav", activate=False,
    )
    assert voice.mode == "clone" and voice.voice_id is None

    organized = organize_avatar(
        avatar, library, reference_audio=voice.audio_filename,
    )
    assert organized == library / "Jorge" / "Jorge.png"
    assert organized.read_bytes() == old_bytes
    assert (organized.parent / "qr.png").read_bytes() == qr.png
    assert json.loads(
        (organized.parent / "qr.json").read_text(encoding="utf-8")
    )["url"] == URL
    assert json.loads(
        (organized.parent / "voice.json").read_text(encoding="utf-8")
    )["audio_filename"] == "muestra.wav"
    assert (organized.parent / "muestra.wav").read_bytes() == b"reference-audio"
    assert not (library / "Jorge.png").exists()
    assert not (library / "muestra.wav").exists()
    assert not (library / ".qr").exists()
    assert not (library / ".voice").exists()
    assert organize_avatar(organized, library) == organized

    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("Migrated assets must not prompt"),
    )
    assert ensure_avatar_qr(
        avatar_file=organized, avatars_dir=library, interactive=True,
    ) == qr
    assert ensure_avatar_voice(
        avatar_file=organized, avatars_dir=library,
        default_voice_id=DEFAULT, interactive=True, activate=False,
    ) == voice
    assert runner.select_avatar(avatars_dir=library, avatar_name="Jorge") == organized


def test_voice_id_survives_migration_and_does_not_reprompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = _avatar(tmp_path)
    voice = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, input_voice_id=CUSTOM,
    )
    organized = organize_avatar(avatar, avatar.parent)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("Voice ID already persisted"),
    )
    assert ensure_avatar_voice(
        avatar_file=organized, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, interactive=True,
    ) == voice
    assert json.loads(
        (organized.parent / "voice.json").read_text(encoding="utf-8")
    )["voice_id"] == CUSTOM


def test_partial_migration_recovers_legacy_metadata_for_nested_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = _avatar(tmp_path)
    qr = ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=avatar.parent, input_url=URL,
    )
    voice = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, input_voice_id=CUSTOM,
    )
    folder = avatar.parent / avatar.stem
    folder.mkdir()
    migrated_png = folder / avatar.name
    avatar.rename(migrated_png)  # Interrupted immediately after moving the PNG.
    organized = organize_avatar(migrated_png, folder.parent)
    assert organized == migrated_png
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("Must reuse legacy metadata"),
    )
    assert ensure_avatar_qr(
        avatar_file=organized, avatars_dir=folder.parent, interactive=True,
    ) == qr
    assert ensure_avatar_voice(
        avatar_file=organized, avatars_dir=folder.parent,
        default_voice_id=DEFAULT, interactive=True,
    ) == voice


def test_migration_refuses_conflicting_file_without_moving_original(
    tmp_path: Path,
) -> None:
    avatar = _avatar(tmp_path)
    ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=avatar.parent, input_url=URL,
    )
    folder = avatar.parent / avatar.stem
    folder.mkdir()
    (folder / "qr.json").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(SystemExit, match="No se sobrescribirá"):
        organize_avatar(avatar, avatar.parent)
    assert avatar.is_file()
    assert (avatar.parent / ".qr" / avatar.name / "qr.json").is_file()
    assert (folder / "qr.json").read_text(encoding="utf-8") == "do not overwrite"


def test_migration_rejects_unexpected_legacy_files(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path)
    slot = avatar.parent / ".qr" / avatar.name
    slot.mkdir(parents=True)
    (slot / "qr.json.part").write_bytes(b"interrupted")
    with pytest.raises(SystemExit, match="sin migrar o temporal"):
        organize_avatar(avatar, avatar.parent)
    assert avatar.is_file()
    assert (slot / "qr.json.part").is_file()


def test_symlinked_legacy_slot_is_rejected(
    tmp_path: Path,
) -> None:
    avatar = _avatar(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    store = avatar.parent / ".qr"
    store.mkdir()
    try:
        (store / avatar.name).symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are not supported on this platform")
    with pytest.raises(SystemExit, match="enlaces"):
        organize_avatar(avatar, avatar.parent)
    assert avatar.is_file()


def test_selector_supports_new_pngs_and_existing_folders_without_hidden_dirs(
    tmp_path: Path,
) -> None:
    first = _avatar(tmp_path, name="Alfa.png")
    folder = first.parent / "Jorge"
    folder.mkdir()
    organized = folder / "Jorge.png"
    Image.new("RGB", (3, 2)).save(organized, format="PNG")
    hidden = first.parent / ".qr" / "old.png"
    hidden.parent.mkdir()
    Image.new("RGB", (3, 2)).save(hidden, format="PNG")
    unrelated = first.parent / "random" / "not-canonical.png"
    unrelated.parent.mkdir()
    Image.new("RGB", (3, 2)).save(unrelated, format="PNG")

    assert runner.available_avatars(first.parent) == [first, organized]
    assert runner.select_avatar(
        avatars_dir=first.parent, avatar_name="Jorge.png",
    ) == organized
    assert runner.select_avatar(
        avatars_dir=first.parent, avatar_name="Alfa",
    ) == first
    duplicate = first.parent / "Jorge.png"
    Image.new("RGB", (3, 2)).save(duplicate, format="PNG")
    with pytest.raises(SystemExit, match="dos avatares"):
        runner.available_avatars(first.parent)


def test_legacy_run_source_can_activate_clone_after_png_is_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = _avatar(tmp_path)
    (avatar.parent / "sample.wav").write_bytes(b"reference")
    pending = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=avatar.parent,
        default_voice_id=DEFAULT, input_audio_filename="sample.wav",
        activate=False,
    )
    old_source = avatar.as_posix()
    organized = organize_avatar(
        avatar, avatar.parent, reference_audio=pending.audio_filename,
    )
    output = tmp_path / "output"
    output.mkdir()
    (output / "avatar_voice.json").write_text(
        json.dumps(pending.snapshot()), encoding="utf-8",
    )
    report: dict[str, object] = {
        "run": {"avatar": {"source": old_source, "voice": pending.snapshot()}}
    }
    (output / "run_report.json").write_text(
        json.dumps(report), encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, _api_key: str) -> None:
            pass

        def clone_voice(self, **_kwargs: object) -> dict[str, str]:
            return {"voice_id": CUSTOM}

    monkeypatch.setattr(
        "ai_video_factory.providers.avatar_voice.AI33SpeechClient", FakeClient,
    )
    monkeypatch.setattr(runner.settings, "ai33_api_key", "fake-key")
    runner._activate_pending_avatar_clone(output, report)
    assert runner._run_voice_id(output) == CUSTOM
    assert json.loads(
        (organized.parent / "voice.json").read_text(encoding="utf-8")
    )["status"] == "ready"


def test_interrupted_move_of_qr_and_voice_finishes_without_reprompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = _avatar(tmp_path)
    library = avatar.parent
    (library / "otra-muestra.wav").write_bytes(b"reference")
    qr = ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=library, input_url=URL,
    )
    selected = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=library,
        default_voice_id=DEFAULT,
        input_audio_filename="otra-muestra.wav", activate=False,
    )
    folder = library / avatar.stem
    folder.mkdir()
    (library / ".qr" / avatar.name / "qr.png").rename(folder / "qr.png")
    (library / ".qr" / avatar.name / "qr.json").rename(folder / "qr.json")
    (library / ".voice" / avatar.name / "voice.json").rename(folder / "voice.json")
    # Simulate interruption before the audio and PNG were moved.
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("Must not ask again"),
    )
    recovered = runner._recover_avatar_layout(avatar, library)
    assert recovered == folder / avatar.name
    assert recovered.is_file() and not avatar.exists()
    assert (folder / "otra-muestra.wav").read_bytes() == b"reference"
    assert ensure_avatar_qr(
        avatar_file=recovered, avatars_dir=library, interactive=True,
    ) == qr
    assert ensure_avatar_voice(
        avatar_file=recovered, avatars_dir=library,
        default_voice_id=DEFAULT, interactive=True, activate=False,
    ) == selected


def test_qr_only_recovers_interrupted_legacy_move_before_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio
    from argparse import Namespace

    avatar = _avatar(tmp_path)
    library = avatar.parent
    qr = ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=library, input_url=URL,
    )
    folder = library / avatar.stem
    folder.mkdir()
    (library / ".qr" / avatar.name / "qr.png").rename(folder / "qr.png")
    (library / ".qr" / avatar.name / "qr.json").rename(folder / "qr.json")
    args = Namespace(
        script_file=None, scripts_dir=tmp_path / "scripts", script=None,
        list_scripts=False, avatars_dir=library, avatar=avatar.name,
        qr_url=None, qr_only=True, list_avatars=False,
    )
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("Saved QR must not reprompt"),
    )
    monkeypatch.setattr(runner.settings, "openai_api_key", None)
    asyncio.run(runner.main())
    assert (folder / avatar.name).is_file()
    assert (folder / "qr.png").read_bytes() == qr.png
    assert "QR listo:" in capsys.readouterr().out
