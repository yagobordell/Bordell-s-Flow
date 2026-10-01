"""Avatar-first managed input discovery, progress folders and verified publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.managed_layout import publish_managed_run, sync_managed_outputs
from ai_video_factory.publishing import PublicationError
from scripts.pipeline import run_managed


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(root: Path, rel: str, contents: bytes) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return _sha(contents)


def _json(root: Path, rel: str, obj: object) -> None:
    _put(root, rel, (json.dumps(obj, ensure_ascii=False) + "\n").encode())


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4)).save(path, format="PNG")


def test_first_raw_png_and_txt_are_organized_and_other_scripts_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    _png(root / "Jorge.png")
    original = b"First line.\r\n\r\n Second line.  "
    _put(root, "test01.txt", original)
    _put(root, "other.txt", b"another pending script")
    png, script = run_managed.prepare_managed_script(
        root, "Jorge", "test01", resume=False
    )
    assert png == root / "Jorge" / "Jorge.png"
    assert png.is_file()
    assert (root / "Jorge" / "meta").is_dir()
    assert script == root / "Jorge" / "test01" / "test01.txt"
    assert script.read_bytes() == original
    assert not (root / "test01.txt").exists()
    assert (root / "other.txt").read_bytes() == b"another pending script"


def test_subsequent_loose_txt_in_avatar_is_moved_on_start(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _png(root / "Jorge" / "Jorge.png")
    pending = root / "Jorge" / "test02.txt"
    pending.write_text("A later script", encoding="utf-8")
    png, script = run_managed.prepare_managed_script(
        root, "Jorge", "test02", resume=False
    )
    assert png == root / "Jorge" / "Jorge.png"
    assert script == root / "Jorge" / "test02" / "test02.txt"
    assert script.read_text(encoding="utf-8") == "A later script"
    assert not pending.exists()


def test_first_run_moves_a_custom_voice_reference_with_the_new_avatar(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    _png(root / "Jorge.png")
    (root / "reference.wav").write_bytes(b"voice reference")
    (root / "first.txt").write_text("Script.", encoding="utf-8")
    avatar, _ = run_managed.prepare_managed_script(
        root, "Jorge", "first", resume=False, reference_audio="reference.wav"
    )
    assert avatar == root / "Jorge" / "Jorge.png"
    assert (root / "Jorge" / "meta" / "reference.wav").read_bytes() == (
        b"voice reference"
    )
    assert not (root / "Jorge" / "reference.wav").exists()
    assert not (root / "reference.wav").exists()


def test_qr_only_does_not_consume_pending_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    _png(root / "Jorge.png")
    (root / "first.txt").write_text("Pending.", encoding="utf-8")
    called: list[list[str]] = []
    monkeypatch.setattr(
        run_managed.subprocess, "call",
        lambda command: called.append(command) or 0,
    )
    assert run_managed.main([
        "--qr-only", "--avatar", "Jorge", "--data-root", str(root),
    ]) == 0
    assert "--qr-only" in called[0]
    assert "--avatar" in called[0]
    assert (root / "first.txt").is_file()
    assert (root / "Jorge" / "meta").is_dir()
    assert not (root / "Jorge" / "first").exists()


def test_existing_script_folder_requires_explicit_resume(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _png(root / "Jorge" / "Jorge.png")
    run = root / "Jorge" / "test01"
    run.mkdir()
    (run / "test01.txt").write_text("saved", encoding="utf-8")
    with pytest.raises(SystemExit, match="already has a run folder"):
        run_managed.prepare_managed_script(root, "Jorge", "test01", resume=False)
    assert run_managed.prepare_managed_script(
        root, "Jorge", "test01", resume=True
    )[1] == run / "test01.txt"


def test_bad_png_or_script_does_not_move_pending_input(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "Jorge.png").write_bytes(b"not a PNG")
    (root / "test01.txt").write_text("good script", encoding="utf-8")
    with pytest.raises(SystemExit, match="Invalid PNG"):
        run_managed.prepare_managed_script(root, "Jorge", "test01", resume=False)
    assert (root / "test01.txt").is_file()
    assert (root / "Jorge.png").is_file()


def test_cannot_overwrite_or_ambiguously_pick_pending_scripts(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _png(root / "Jorge.png")
    (root / "Jorge").mkdir()
    (root / "test01.txt").write_text("first", encoding="utf-8")
    (root / "Jorge" / "test01.txt").write_text("different", encoding="utf-8")
    with pytest.raises(SystemExit, match="Ambiguous duplicate"):
        run_managed.prepare_managed_script(root, "Jorge", "test01", resume=False)
    assert (root / "test01.txt").exists()


def test_existing_avatar_does_not_claim_txt_left_in_data_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    _png(root / "Jorge" / "Jorge.png")
    (root / "orphan.txt").write_text("unassigned", encoding="utf-8")
    assert run_managed._script_options(root, "Jorge", resume=False) == []
    with pytest.raises(SystemExit, match="place orphan.txt"):
        run_managed.prepare_managed_script(root, "Jorge", "orphan", resume=False)
    assert (root / "orphan.txt").is_file()


def test_launcher_passes_managed_target_and_resume_without_paid_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    _png(root / "Jorge.png")
    (root / "test01.txt").write_text("My script.", encoding="utf-8")
    work = tmp_path / ".video_factory_work"
    monkeypatch.setattr(run_managed, "WORK_ROOT", work)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        run_managed.subprocess, "call",
        lambda command: commands.append(command) or 0,
    )
    assert run_managed.main([
        "test01", "Jorge", "--data-root", str(root), "--no-image",
    ]) == 0
    assert len(commands) == 1
    assert "--managed-target" in commands[0]
    assert str(root / "Jorge" / "test01") in commands[0]
    assert "--avatar" in commands[0]
    assert "--output" in commands[0]
    assert not (root / "test01.txt").exists()
    assert (root / "Jorge" / "test01" / "test01.txt").exists()

    commands.clear()
    assert run_managed.main([
        "test01", "Jorge", "--data-root", str(root), "--generate-video",
    ]) == 0
    assert "--generate-video" in commands[0]
    assert "--avatar" not in commands[0]


@pytest.fixture
def managed_complete(tmp_path: Path) -> tuple[Path, Path, Path]:
    avatar_dir = tmp_path / "data" / "Jorge"
    target = avatar_dir / "test01"
    target.mkdir(parents=True)
    _png(avatar_dir / "Jorge.png")
    script = target / "test01.txt"
    script.write_bytes(b"Original UTF8 script.")
    work = tmp_path / ".video_factory_work" / "Jorge" / "test01"
    work.mkdir(parents=True)
    for name in ("B1.1", "B1.2", "B2"):
        _json(work, f"{name}/block_1/input.json", {"stage": name})
        _json(work, f"{name}/block_1/output.json", {"stage": name})
    avatar_sha = _put(work, "avatar.png", b"avatar image bytes")
    qr_sha = _put(work, "qr.png", b"qr image bytes")
    _json(work, "avatar_voice.json", {"voice_id": "x"})
    _json(work, "visual_plan.json", {
        "avatar": {
            "filename": "Jorge.png", "file": "avatar.png", "sha256": avatar_sha,
            "qr": {"file": "qr.png", "sha256": qr_sha},
        },
    })
    image_sha = _put(work, "images/block_1/1A.png", b"generated image bytes")
    _json(work, "images/manifest.json", {
        "status": "completed", "items": [{
            "file": "images/block_1/1A.png", "sha256": image_sha,
        }],
    })
    block_sha = _put(work, "audio/runs/a/blocks/block_1.wav", b"tts block")
    audio_sha = _put(work, "audio/runs/a/narration.wav", b"master audio")
    video_sha = _put(work, "video/runs/v/final.mp4", b"final MP4 bytes")
    _json(work, "run_report.json", {
        "run": {
            "status": "completed", "script_sha256": _sha(script.read_bytes()),
            "audio_generation": {
                "status": "completed", "file": "audio/runs/a/narration.wav",
                "sha256": audio_sha, "blocks": [{
                    "block_id": 1, "file": "audio/runs/a/blocks/block_1.wav",
                    "sha256": block_sha,
                }],
            },
            "video_generation": {
                "status": "completed", "file": "video/runs/v/final.mp4",
                "sha256": video_sha,
            },
        },
    })
    return work, script, target


def test_partial_run_exposes_bot_outputs_before_final_video(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    sync_managed_outputs(work, target)
    assert (target / "outputs" / "B1.1" / "block_1" / "input.json").is_file()
    assert (target / "outputs" / "B2" / "block_1" / "output.json").is_file()
    assert not (target / "manifest.json").exists()


def test_published_folder_has_exact_requested_layout_and_is_idempotent(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, script, target = managed_complete
    sync_managed_outputs(work, target)
    assert publish_managed_run(work, script, target) == target
    assert (target / "test01.txt").read_bytes() == b"Original UTF8 script."
    assert (target / "outputs" / "B1.1" / "block_1" / "input.json").is_file()
    assert (target / "outputs" / "B1.2" / "block_1" / "output.json").is_file()
    assert (target / "outputs" / "B2" / "block_1" / "output.json").is_file()
    assert (target / "media" / "images" / "block_1" / "1A.png").is_file()
    assert (target / "media" / "audio" / "narracion.wav").is_file()
    assert (target / "media" / "audio" / "bloques" / "bloque_1.wav").is_file()
    assert (target / "test01.mp4").read_bytes() == b"final MP4 bytes"
    assert (target / "manifest.json").is_file()
    assert publish_managed_run(work, script, target) == target
    sync_managed_outputs(work, target)
    assert len(list(target.glob("*.mp4"))) == 1
    assert (work / "video" / "runs" / "v" / "final.mp4").exists()


def test_different_run_cannot_overwrite_finished_script_folder(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, script, target = managed_complete
    publish_managed_run(work, script, target)
    new_sha = _put(work, "video/runs/v/final.mp4", b"other video")
    report = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    report["run"]["video_generation"]["sha256"] = new_sha
    _json(work, "run_report.json", report)
    with pytest.raises(PublicationError, match="different completed generation"):
        publish_managed_run(work, script, target)
    assert (target / "test01.mp4").read_bytes() == b"final MP4 bytes"


def test_tampered_published_file_is_rejected(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, script, target = managed_complete
    publish_managed_run(work, script, target)
    (target / "test01.mp4").write_bytes(b"tampered")
    with pytest.raises(PublicationError, match="changed or missing"):
        publish_managed_run(work, script, target)



def test_first_run_meta_migrates_existing_qr_voice_and_custom_audio(
    tmp_path: Path,
) -> None:
    from ai_video_factory.providers.avatar_library import (
        ensure_avatar_meta,
        organize_avatar,
    )

    root = tmp_path / "data"
    root.mkdir()
    avatar = root / "Jorge.png"
    _png(avatar)
    (root / "sample.wav").write_bytes(b"reference")
    (root / ".qr" / avatar.name).mkdir(parents=True)
    (root / ".voice" / avatar.name).mkdir(parents=True)
    (root / ".qr" / avatar.name / "qr.png").write_bytes(b"qr")
    (root / ".qr" / avatar.name / "qr.json").write_text(
        "{}", encoding="utf-8"
    )
    (root / ".voice" / avatar.name / "voice.json").write_text(
        json.dumps({"mode": "clone", "audio_filename": "sample.wav"}),
        encoding="utf-8",
    )
    migrated = organize_avatar(avatar, root, reference_audio="sample.wav")
    meta = ensure_avatar_meta(migrated, root, reference_audio="sample.wav")
    assert (meta / "qr.png").read_bytes() == b"qr"
    assert (meta / "qr.json").is_file()
    assert (meta / "voice.json").is_file()
    assert (meta / "sample.wav").read_bytes() == b"reference"
    assert not (migrated.parent / "qr.png").exists()
    assert not (migrated.parent / "voice.json").exists()
    assert not (migrated.parent / "sample.wav").exists()
    assert ensure_avatar_meta(migrated, root) == meta


def test_managed_qr_and_voice_persist_inside_meta_without_paid_calls(
    tmp_path: Path,
) -> None:
    from ai_video_factory.providers.avatar_qr import ensure_avatar_qr
    from ai_video_factory.providers.avatar_voice import ensure_avatar_voice

    root = tmp_path / "data"
    root.mkdir()
    _png(root / "Jorge.png")
    (root / "first.txt").write_text("First pending.", encoding="utf-8")
    avatar, _ = run_managed.prepare_managed_script(
        root, "Jorge", "first", resume=False
    )
    meta = avatar.parent / "meta"
    qr = ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=root,
        input_url="https://example.com/ebook", interactive=False,
    )
    voice = ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=root,
        default_voice_id="fishaudio_80e34d5e0b2b4577a486f3a77e357261",
        use_default=True, activate=False,
    )
    assert (meta / "qr.png").read_bytes() == qr.png
    assert (meta / "qr.json").is_file()
    assert voice.mode == "default"
    assert (meta / "voice.json").is_file()
    assert not (avatar.parent / "qr.png").exists()
    assert not (avatar.parent / "voice.json").exists()
    assert ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=root, interactive=False,
    ) == qr
    assert ensure_avatar_voice(
        avatar_file=avatar, avatars_dir=root,
        default_voice_id="fishaudio_80e34d5e0b2b4577a486f3a77e357261",
        interactive=False, activate=False,
    ) == voice
