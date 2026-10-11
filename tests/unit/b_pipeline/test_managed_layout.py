"""Avatar-first managed input discovery, progress folders and verified publication."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory import managed_media
from ai_video_factory.managed_layout import publish_managed_run, sync_managed_outputs
from ai_video_factory.managed_media import public_report
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


def test_retired_legacy_layout_flag_fails_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_call(*args, **kwargs):
        raise AssertionError("Legacy layout must not run a subprocess")

    monkeypatch.setattr(run_managed.subprocess, "call", unexpected_call)
    with pytest.raises(SystemExit, match="has been retired"):
        run_managed.main(["--legacy-layout", "test01", "Jorge"])


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


def test_explicit_rerun_archives_existing_visible_and_internal_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    _png(root / "Enzo" / "Enzo.png")
    existing = root / "Enzo" / "test"
    existing.mkdir()
    (existing / "test.txt").write_text("Original script.", encoding="utf-8")
    (existing / "old-output.txt").write_text("keep me", encoding="utf-8")
    work = tmp_path / ".video_factory_work"
    old_work = work / "Enzo" / "test"
    old_work.mkdir(parents=True)
    (old_work / "run_report.json").write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(run_managed, "WORK_ROOT", work)
    monkeypatch.setattr(run_managed, "sync_managed_outputs", lambda *_: None)
    monkeypatch.setattr(
        run_managed.subprocess, "call", lambda command: calls.append(command) or 0,
    )

    assert run_managed.main([
        "test", "Enzo", "--rerun", "--data-root", str(root),
    ]) == 0

    assert (root / "Enzo" / "test" / "test.txt").read_text(encoding="utf-8") == (
        "Original script."
    )
    history = list((root / "Enzo" / ".history").iterdir())
    assert len(history) == 1
    assert (history[0] / "old-output.txt").read_text(encoding="utf-8") == "keep me"
    assert list((work / "Enzo" / ".history").iterdir())
    assert "--rerun" not in calls[0]


def test_resume_infers_avatar_from_owning_folder_not_loose_ebook_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    _png(root / "cover_playbook.png")
    _png(root / "Layla" / "Layla.png")
    saved = root / "Layla" / "bodas_15_costes_ocultos"
    saved.mkdir()
    script = saved / "bodas_15_costes_ocultos.txt"
    script.write_text("Already started.", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(run_managed, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(run_managed, "sync_managed_outputs", lambda *_: None)
    monkeypatch.setattr(
        run_managed.subprocess, "call", lambda cmd: calls.append(cmd) or 0,
    )
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("Known script must not ask for an avatar"),
    )

    assert run_managed.main([
        "bodas_15_costes_ocultos", "--from", "B2", "--data-root", str(root),
    ]) == 0
    assert len(calls) == 1
    assert "--avatar" in calls[0]
    assert calls[0][calls[0].index("--avatar") + 1] == "Layla"
    assert str(script) in calls[0]
    assert (root / "cover_playbook.png").is_file()


def test_existing_avatar_pending_script_is_selected_without_cover_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    _png(root / "cover_playbook.png")
    _png(root / "Layla" / "Layla.png")
    pending = root / "Layla" / "new_video.txt"
    pending.write_text("New script.", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(run_managed, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(run_managed, "sync_managed_outputs", lambda *_: None)
    monkeypatch.setattr(
        run_managed.subprocess, "call", lambda cmd: calls.append(cmd) or 0,
    )
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("Known script must not ask for an avatar"),
    )

    assert run_managed.main([
        "new_video", "--no-image", "--data-root", str(root),
    ]) == 0
    assert len(calls) == 1
    assert calls[0][calls[0].index("--avatar") + 1] == "Layla"
    assert (root / "Layla" / "new_video" / "new_video.txt").is_file()
    assert (root / "cover_playbook.png").is_file()


def test_registered_ebook_cover_is_not_listed_as_managed_avatar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "data"
    _png(root / "cover_playbook.png")
    _png(root / "Layla" / "Layla.png")
    meta = root / "Layla" / "meta"
    meta.mkdir()
    (meta / "ebook_covers.json").write_text(
        json.dumps({
            "schema_version": "ebook-covers-v1",
            "avatar": "Layla.png",
            "covers": [{"name": "cover_playbook", "sha256": "test"}],
        }), encoding="utf-8",
    )
    assert run_managed.main(["--list-avatars", "--data-root", str(root)]) == 0
    assert capsys.readouterr().out.strip() == "Layla"


def test_resume_same_script_under_multiple_avatars_requires_real_owner_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    _png(root / "cover_playbook.png")
    for avatar in ("Layla", "Eva"):
        _png(root / avatar / (avatar + ".png"))
        run = root / avatar / "shared"
        run.mkdir()
        (run / "shared.txt").write_text("Started.", encoding="utf-8")
    choices: list[list[str]] = []

    def choose(options: list[str], kind: str) -> str:
        assert kind == "avatars"
        choices.append(options)
        return "Layla"

    monkeypatch.setattr(run_managed, "_choose", choose)
    monkeypatch.setattr(run_managed, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(run_managed, "sync_managed_outputs", lambda *_: None)
    monkeypatch.setattr(run_managed.subprocess, "call", lambda _: 0)
    assert run_managed.main([
        "shared", "--from", "B2", "--data-root", str(root),
    ]) == 0
    assert choices == [["Eva", "Layla"]]


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
    _json(work, "B3/group_1/input.json", {"stage": "B3"})
    _json(work, "B3/group_1/output.json", {"stage": "B3"})
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
    _json(work, "video/runs/v/manifest.json", {
        "status": "completed",
        "file": "video/runs/v/final.mp4",
        "sha256": video_sha,
        "master_audio_sha256": audio_sha,
    })
    _json(work, "run_report.json", {
        "run": {
            "status": "completed", "script_sha256": _sha(script.read_bytes()),
            "image_generation": {"status": "completed", "count": 1},
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


def test_public_report_stage_order_is_canonical() -> None:
    report = {
        "run": {
            "status": "completed",
            "audio_generation": {"status": "completed"},
            "image_generation": {"status": "completed"},
            "video_preview": {"status": "completed"},
            "video_generation": {"status": "completed"},
        }
    }
    first = public_report(
        report,
        {
            "audio": "completed",
            "images": "completed",
            "preview": "completed",
            "video": "completed",
        },
        "test01",
    )
    second = public_report(
        report,
        {
            "audio": "completed",
            "images": "completed",
            "video": "completed",
            "preview": "completed",
        },
        "test01",
    )

    assert first == second
    published = json.loads(first)["run"]["published_media"]
    assert list(published) == ["audio", "images", "preview", "video"]


def test_partial_run_exposes_bot_outputs_before_final_video(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    sync_managed_outputs(work, target)
    assert (target / "outputs" / "B1.1" / "block_1" / "input.json").is_file()
    assert (target / "outputs" / "B2" / "block_1" / "output.json").is_file()
    assert (target / "outputs" / "B3" / "group_1" / "input.json").is_file()
    assert (target / "outputs" / "B3" / "group_1" / "output.json").is_file()
    assert not (target / "manifest.json").exists()


def test_managed_outputs_retries_transient_windows_rename_and_preserves_old_snapshot(
    managed_complete: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory import managed_layout

    work, _script, target = managed_complete
    sync_managed_outputs(work, target)
    old = (target / "outputs" / "B2" / "block_1" / "output.json").read_bytes()
    new = b'{"stage":"B2","revision":2}'
    (work / "B2" / "block_1" / "output.json").write_bytes(new)

    original_rename = Path.rename
    failures = 0
    sleeps: list[float] = []

    def flaky_rename(source: Path, destination: Path) -> Path:
        nonlocal failures
        if (
            source.name.startswith(".outputs-")
            and not source.name.startswith(".outputs-backup-")
            and destination.name == "outputs"
            and failures < 2
        ):
            failures += 1
            # The previous published partial snapshot must not be destroyed.
            backup = list(target.glob(".outputs-backup-*.partial"))
            assert len(backup) == 1
            assert (backup[0] / "B2" / "block_1" / "output.json").read_bytes() == old
            raise PermissionError(13, "Transient Windows indexer lock")
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    monkeypatch.setattr(managed_layout.time, "sleep", sleeps.append)
    sync_managed_outputs(work, target)

    assert failures == 2
    assert sleeps == [0.1, 0.25]
    assert (target / "outputs" / "B2" / "block_1" / "output.json").read_bytes() == new
    assert not list(target.glob(".outputs-backup-*.partial"))
    assert not list(target.glob(".outputs-*.partial"))


def test_managed_outputs_restores_old_snapshot_if_windows_rename_never_recovers(
    managed_complete: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory import managed_layout

    work, _script, target = managed_complete
    sync_managed_outputs(work, target)
    old = (target / "outputs" / "B1.2" / "block_1" / "output.json").read_bytes()
    (work / "B1.2" / "block_1" / "output.json").write_bytes(b"replacement")

    original_rename = Path.rename

    def blocked_rename(source: Path, destination: Path) -> Path:
        if (
            source.name.startswith(".outputs-")
            and not source.name.startswith(".outputs-backup-")
            and destination.name == "outputs"
        ):
            raise PermissionError(13, "Windows folder in use")
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", blocked_rename)
    monkeypatch.setattr(managed_layout.time, "sleep", lambda _seconds: None)
    with pytest.raises(PermissionError, match="Windows folder in use"):
        sync_managed_outputs(work, target)

    assert (target / "outputs" / "B1.2" / "block_1" / "output.json").read_bytes() == old
    assert not list(target.glob(".outputs-backup-*.partial"))
    assert not list(target.glob(".outputs-*.partial"))
    # A later sync can recover without returning to audio/images/LTX.
    monkeypatch.setattr(Path, "rename", original_rename)
    sync_managed_outputs(work, target)
    assert (target / "outputs" / "B1.2" / "block_1" / "output.json").read_bytes() == b"replacement"


def test_completed_video_publishes_after_recovering_missing_outputs_directory(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, script, target = managed_complete
    sync_managed_outputs(work, target)
    # Mirrors the previous Windows implementation: outputs was deleted before
    # the staged directory rename failed.
    shutil.rmtree(target / "outputs")
    assert publish_managed_run(work, script, target) == target
    assert (target / "test01.mp4").read_bytes() == b"final MP4 bytes"
    assert (target / "manifest.json").is_file()


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



def _set_run(work: Path, **stages: object) -> None:
    report_path = work / "run_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["run"].update(stages)
    _json(work, "run_report.json", report)


def test_audio_only_exposes_real_audio_and_valid_report_paths(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    (work / "images" / "manifest.json").unlink()
    _set_run(
        work,
        image_generation={"status": "skipped"},
        video_generation={"status": "deferred"},
        status="completed",
    )
    sync_managed_outputs(work, target)
    assert (target / "media" / "audio" / "narracion.wav").read_bytes() == b"master audio"
    assert (
        target / "media" / "audio" / "bloques" / "bloque_1.wav"
    ).read_bytes() == b"tts block"
    assert not (target / "test01.mp4").exists()
    assert not (target / "manifest.json").exists()
    report = json.loads(
        (target / "outputs" / "run_report.json").read_text(encoding="utf-8")
    )
    assert report["run"]["internal_status"] == "completed"
    assert report["run"]["status"] == "partial"
    assert report["run"]["published_media"] == {"audio": "completed"}
    assert report["run"]["audio_generation"]["file"] == "media/audio/narracion.wav"
    for block in report["run"]["audio_generation"]["blocks"]:
        assert (target / block["file"]).is_file()
    assert (work / "audio" / "runs" / "a" / "narration.wav").is_file()


def test_regenerated_audio_replaces_only_verified_visible_audio(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    _set_run(work, video_generation={"status": "deferred"})
    sync_managed_outputs(work, target)
    first = target / "media" / "audio" / "narracion.wav"
    assert first.read_bytes() == b"master audio"
    latest_sha = _put(
        work, "audio/runs/a/narration_audio_regen_1.wav", b"regenerated narration"
    )
    latest_block = _put(
        work, "audio/runs/a/blocks/regenerated.wav", b"regenerated block"
    )
    report_path = work / "run_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    audio = report["run"]["audio_generation"]
    audio.update(
        file="audio/runs/a/narration_audio_regen_1.wav", sha256=latest_sha
    )
    audio["blocks"][0].update(
        file="audio/runs/a/blocks/regenerated.wav", sha256=latest_block
    )
    report["run"]["video_preview"] = {"status": "stale_audio_regeneration"}
    _json(work, "run_report.json", report)
    sync_managed_outputs(work, target)
    assert first.read_bytes() == b"regenerated narration"
    assert (
        target / "media" / "audio" / "bloques" / "bloque_1.wav"
    ).read_bytes() == b"regenerated block"
    assert (work / "audio" / "runs" / "a" / "narration.wav").read_bytes() == (
        b"master audio"
    )
    assert (target / "media" / "audio" / "manifest.json").is_file()
    public = json.loads((target / "outputs" / "run_report.json").read_text())
    assert public["run"]["audio_generation"]["file"] == "media/audio/narracion.wav"
    assert public["run"]["published_media"]["audio"] == "completed"
    assert public["run"]["published_media"].get("preview") is None


def test_images_are_visible_without_audio_or_full_video(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    _set_run(
        work,
        audio_generation={"status": "skipped"},
        video_generation={"status": "deferred"},
    )
    sync_managed_outputs(work, target)
    assert (target / "media" / "images" / "block_1" / "1A.png").read_bytes() == (
        b"generated image bytes"
    )
    stage = json.loads(
        (target / "media" / "images" / "manifest.json").read_text()
    )
    assert stage["status"] == "completed"
    assert stage["files"]["block_1/1A.png"] == _sha(b"generated image bytes")
    assert (target / "outputs" / "run_report.json").is_file()
    assert not (target / "test01.mp4").exists()


def test_static_image_video_is_visible_before_ltx(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    preview_sha = _put(work, "preview/static_preview.mp4", b"verified static preview")
    _put(work, "preview/timeline.json", b'{"beats":[]}')
    preview = {
        "status": "completed",
        "file": "preview/static_preview.mp4",
        "sha256": preview_sha,
    }
    _json(work, "preview/manifest.json", {
        **preview,
        "timeline_file": "preview/timeline.json",
        "audio_file": "audio/runs/a/narration.wav",
    })
    _set_run(
        work,
        video_generation={"status": "deferred"},
        video_preview=preview,
    )
    sync_managed_outputs(work, target)
    assert (target / "media" / "preview" / "preview.mp4").read_bytes() == (
        b"verified static preview"
    )
    assert (target / "media" / "preview" / "timeline.json").is_file()
    report = json.loads((target / "outputs" / "run_report.json").read_text())
    assert report["run"]["video_preview"]["file"] == "media/preview/preview.mp4"
    assert report["run"]["video_generation"]["status"] == "deferred"
    assert not (target / "test01.mp4").exists()


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("audio/runs/a/narration.wav", "audio"),
        ("audio/runs/a/blocks/block_1.wav", "audio"),
        ("images/block_1/1A.png", "image"),
        ("video/runs/v/final.mp4", "video"),
    ],
)
def test_missing_completed_artifacts_do_not_publish_successful_report(
    managed_complete: tuple[Path, Path, Path], relative: str, expected: str,
) -> None:
    work, _script, target = managed_complete
    (work / relative).unlink()
    with pytest.raises(PublicationError, match="Missing|escaping"):
        sync_managed_outputs(work, target)
    assert not (target / "outputs" / "run_report.json").exists()
    assert not (target / "manifest.json").exists()
    if expected == "audio":
        assert not (target / "media" / "audio" / "manifest.json").exists()


def test_visible_audio_tampering_fails_instead_of_overwriting(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    _set_run(work, video_generation={"status": "deferred"})
    sync_managed_outputs(work, target)
    published = target / "media" / "audio" / "narracion.wav"
    published.write_bytes(b"tampered")
    with pytest.raises(PublicationError, match="missing or modified"):
        sync_managed_outputs(work, target)
    assert published.read_bytes() == b"tampered"
    assert not (target / "manifest.json").exists()



def test_offline_sync_only_recovers_audio_without_paid_calls(
    managed_complete: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, _script, target = managed_complete
    _set_run(
        work,
        image_generation={"status": "skipped"},
        video_generation={"status": "deferred"},
    )
    (work / "images" / "manifest.json").unlink()
    monkeypatch.setattr(run_managed, "WORK_ROOT", work.parent.parent)
    monkeypatch.setattr(
        run_managed.subprocess, "call",
        lambda _command: pytest.fail("--sync-only must not invoke any provider"),
    )
    assert run_managed.main([
        "test01", "Jorge", "--data-root", str(target.parent.parent), "--sync-only",
    ]) == 0
    assert (target / "media" / "audio" / "narracion.wav").is_file()
    assert (target / "outputs" / "run_report.json").is_file()


def test_offline_sync_only_rejects_absent_internal_data(
    managed_complete: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _work, _script, target = managed_complete
    monkeypatch.setattr(run_managed, "WORK_ROOT", tmp_path / "missing")
    monkeypatch.setattr(
        run_managed.subprocess, "call",
        lambda _command: pytest.fail("Do not regenerate paid files"),
    )
    with pytest.raises(SystemExit, match="Missing files cannot be recovered"):
        run_managed.main([
            "test01", "Jorge", "--data-root", str(target.parent.parent), "--sync-only",
        ])


def test_audio_resume_requires_every_original_paid_block_file(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    from scripts.pipeline.run_b_pipeline import _latest_completed_audio

    work, script, _target = managed_complete
    artifact = json.loads((work / "run_report.json").read_text())[
        "run"
    ]["audio_generation"]
    artifact["run_id"] = "a"
    _json(work, "audio/latest.json", artifact)
    _json(work, "audio/runs/a/task.json", {
        "status": "completed",
        "run_id": "a",
        "source_sha256": _sha(script.read_bytes()),
        "audio": artifact,
    })
    assert _latest_completed_audio(work, _sha(script.read_bytes())) == artifact
    (work / "audio" / "runs" / "a" / "blocks" / "block_1.wav").unlink()
    assert _latest_completed_audio(work, _sha(script.read_bytes())) is None



def test_sync_detects_tampered_finished_publication(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, script, target = managed_complete
    publish_managed_run(work, script, target)
    (target / "media" / "audio" / "narracion.wav").unlink()
    with pytest.raises(PublicationError, match="changed or missing"):
        sync_managed_outputs(work, target)


def test_sync_never_claims_old_mp4_matches_regenerated_audio(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, script, target = managed_complete
    publish_managed_run(work, script, target)
    new_sha = _put(
        work, "audio/runs/a/narration_audio_regen_1.wav", b"new narration"
    )
    report = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    report["run"]["audio_generation"].update(
        file="audio/runs/a/narration_audio_regen_1.wav", sha256=new_sha
    )
    _json(work, "run_report.json", report)
    with pytest.raises(PublicationError, match="internal narration has changed"):
        sync_managed_outputs(work, target)
    assert (target / "test01.mp4").read_bytes() == b"final MP4 bytes"



def test_completed_audio_remains_visible_when_later_pipeline_stage_fails(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    _set_run(
        work,
        status="failed",
        image_generation={"status": "incomplete"},
        video_preview={"status": "incomplete"},
        video_generation={"status": "incomplete"},
    )
    (work / "images" / "manifest.json").unlink()
    sync_managed_outputs(work, target)
    assert (target / "media" / "audio" / "narracion.wav").read_bytes() == (
        b"master audio"
    )
    assert (target / "media" / "audio" / "bloques" / "bloque_1.wav").is_file()
    public = json.loads((target / "outputs" / "run_report.json").read_text())
    assert public["run"]["internal_status"] == "failed"
    assert public["run"]["status"] == "partial"
    assert public["run"]["published_media"]["audio"] == "completed"


def test_claimed_completed_preview_with_missing_video_is_rejected(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    preview_sha = _put(work, "preview/preview.mp4", b"static preview")
    _put(work, "preview/timeline.json", b"{}")
    _json(work, "preview/manifest.json", {
        "status": "completed",
        "file": "preview/preview.mp4",
        "sha256": preview_sha,
        "timeline_file": "preview/timeline.json",
        "audio_file": "audio/runs/a/narration.wav",
    })
    _set_run(
        work,
        video_generation={"status": "deferred"},
        video_preview={
            "status": "completed",
            "file": "preview/preview.mp4",
            "sha256": preview_sha,
        },
    )
    (work / "preview" / "preview.mp4").unlink()
    with pytest.raises(PublicationError, match="Missing|escaping"):
        sync_managed_outputs(work, target)
    assert not (target / "outputs" / "run_report.json").exists()
    assert not (target / "media" / "preview" / "manifest.json").exists()


def test_saved_ltx_video_is_rejected_if_it_uses_other_narration(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, _script, target = managed_complete
    manifest = json.loads(
        (work / "video" / "runs" / "v" / "manifest.json").read_text()
    )
    manifest["master_audio_sha256"] = _sha(b"stale old narration")
    _json(work, "video/runs/v/manifest.json", manifest)
    with pytest.raises(PublicationError, match="does not match its LTX/audio"):
        sync_managed_outputs(work, target)
    assert not (target / "test01.mp4").exists()
    assert not (target / "outputs" / "run_report.json").exists()



def test_professional_v_stages_are_visible_next_to_b_bots_and_in_final_publication(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, script, target = managed_complete
    _json(work, "V1/input.json", {"payload": "direction"})
    _json(work, "V1/output.json", {"pipeline_stage": "V1"})
    _json(work, "V2/block_1/input.json", {"payload": "rhythm"})
    _json(work, "V2/block_1/output.json", {"pipeline_stage": "V2"})
    _json(work, "V3/block_1/input.json", {"payload": "delivery"})
    _json(work, "V3/block_1/output.json", {"pipeline_stage": "V3"})
    sync_managed_outputs(work, target)
    for stage, rel in (
        ("V1", "input.json"), ("V1", "output.json"),
        ("V2", "block_1/output.json"),
        ("V3", "block_1/input.json"),
        ("V3", "block_1/output.json"),
    ):
        assert (target / "outputs" / stage / rel).is_file()
    publish_managed_run(work, script, target)
    assert (target / "outputs" / "V3" / "block_1" / "output.json").is_file()
    published = json.loads((target / "manifest.json").read_text())
    assert any(key.startswith("outputs/V1/") for key in published["files"])
    assert any(key.startswith("outputs/V2/") for key in published["files"])
    assert any(key.startswith("outputs/V3/") for key in published["files"])


def test_legacy_v_checkpoints_are_exported_without_mutating_old_run(
    managed_complete: tuple[Path, Path, Path],
) -> None:
    work, script, target = managed_complete
    _json(work, "voice_direction/V1/pass_1/input.json", {"legacy": True})
    _json(work, "voice_direction/V1/pass_1/output.json", {"legacy": True})
    sync_managed_outputs(work, target)
    assert (target / "outputs" / "V1" / "pass_1" / "input.json").is_file()
    publish_managed_run(work, script, target)
    assert (target / "outputs" / "V1" / "pass_1" / "output.json").is_file()
    assert (work / "voice_direction" / "V1" / "pass_1" / "output.json").is_file()


def test_professional_finished_subshots_match_b2_by_asset_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"
    first = b"finished-subshot-one"
    second = b"finished-subshot-two"
    avatar = b"avatar"
    first_sha = _put(work, "images/12A__1.png", first)
    second_sha = _put(work, "images/12A__2.png", second)
    avatar_sha = _put(work, "avatar.png", avatar)

    _json(work, "visual_plan.json", {
        "visual_style": "profesional",
        "avatar": {"file": "avatar.png", "sha256": avatar_sha},
        "blocks": [{"block_id": 12, "beats": [{"beat_id": "12A"}]}],
    })
    final = [
        {
            "block_id": 12, "beat_id": "12A", "asset_id": "12A__1",
            "visual_type": "media_image", "file": "images/12A__1.png",
            "sha256": first_sha,
        },
        {
            "block_id": 12, "beat_id": "12A", "asset_id": "12A__2",
            "visual_type": "media_image", "file": "images/12A__2.png",
            "sha256": second_sha,
        },
    ]
    _json(work, "images/manifest.json", {
        "status": "completed",
        "total_generation_jobs": 2,
        "items": [
            {**final[0], "provider": "openai_direct"},
            {**final[1], "provider": "openai_direct"},
        ],
        "panel_assets": [],
        "finishing_status": "completed",
        "final_assets": final,
    })
    monkeypatch.setattr(
        managed_media,
        "described_beats",
        lambda _plan: [
            {"block_id": 12, "beat_id": "12A", "asset_id": "12A__1"},
            {"block_id": 12, "beat_id": "12A", "asset_id": "12A__2"},
        ],
    )

    status, entries = managed_media._image_sources(
        work,
        {"image_generation": {"status": "completed", "count": 2}},
    )

    assert status == "completed"
    assert set(entries) == {"12A__1.png", "12A__2.png", "avatar.png"}
