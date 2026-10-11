"""Managed, avatar-first layout for pending scripts and completed deliverables.

Raw .txt files in an avatar folder are pending. Starting one moves the exact
source bytes into its own named folder; pipeline recovery remains elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from uuid import uuid4

from ai_video_factory.managed_media import public_report, sync_managed_media
from ai_video_factory.publishing import PublicationError, _collect, _digest, _json

_LOGGER = logging.getLogger(__name__)
_RENAME_RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0, 2.0)


def _rename_directory_with_retries(source: Path, destination: Path) -> None:
    """Retry transient Windows directory locks without hiding permanent failures."""
    for delay in (*_RENAME_RETRY_DELAYS, None):
        try:
            source.rename(destination)
            return
        except OSError as exc:
            if not (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32, 33}
            ) or delay is None:
                raise
            time.sleep(delay)


def _replace_partial_outputs(staging: Path, current: Path) -> None:
    """Install a new snapshot without destroying the old one before commit.

    On Windows, Explorer/indexers/virus scanners can briefly prevent directory
    renames after a previous outputs snapshot is moved. Keep an intact backup
    until the staged snapshot has been installed and restore it on failure.
    """
    _check_dir(current)
    backup = current.parent / (".outputs-backup-" + uuid4().hex + ".partial")
    if backup.exists() or backup.is_symlink():
        raise PublicationError(f"Managed output backup already exists: {backup}")
    moved_previous = False
    installed = False
    try:
        if current.exists():
            _rename_directory_with_retries(current, backup)
            moved_previous = True
        _rename_directory_with_retries(staging, current)
        installed = True
    except Exception:
        if moved_previous:
            try:
                _rename_directory_with_retries(backup, current)
            except OSError as rollback_exc:
                raise PublicationError(
                    "Could not restore the previous managed outputs after a "
                    f"Windows rename failure. Original outputs remain at {backup}; "
                    "do not delete that backup. Close processes using the outputs "
                    "folder and retry --publish-only."
                ) from rollback_exc
        raise
    finally:
        if installed and moved_previous and backup.is_dir():
            try:
                shutil.rmtree(backup)
            except OSError as cleanup_exc:
                _LOGGER.warning(
                    "Published outputs installed, but previous backup %s "
                    "could not be removed: %s", backup, cleanup_exc,
                )


def _check_dir(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise PublicationError(f"Linked managed directory: {path}")
    if path.exists() and not path.is_dir():
        raise PublicationError(f"Expected managed directory: {path}")


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or source.is_junction() or not source.is_dir():
        raise PublicationError(f"Unsafe checkpoint directory: {source}")
    for item in sorted(source.rglob("*")):
        if item.is_symlink() or item.is_junction():
            raise PublicationError(f"Linked checkpoint asset: {item}")
        if item.is_dir():
            (destination / item.relative_to(source)).mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target = destination / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target, follow_symlinks=False)


def sync_managed_outputs(work: Path, target: Path) -> None:
    """Expose available bot checkpoints after a stopped or interrupted run.

    A finished, published result is never mutated by partial recovery sync.
    """
    _check_dir(target)
    marker = target / "manifest.json"
    if marker.is_symlink():
        raise PublicationError("Linked final publication manifest")
    if marker.exists():
        if not marker.is_file():
            raise PublicationError("Invalid final publication manifest")
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or not isinstance(saved.get("files"), dict):
            raise PublicationError("Invalid final publication file records")
        _verify(target, saved)
        current_report = work / "run_report.json"
        if current_report.is_file() and not current_report.is_symlink():
            report = _json(work, "run_report.json")
            run = report.get("run")
            audio = run.get("audio_generation") if isinstance(run, dict) else None
            if isinstance(audio, dict) and audio.get("status") == "completed" and (
                saved["files"].get("media/audio/narracion.wav") != audio.get("sha256")
            ):
                raise PublicationError(
                    "The internal narration has changed since the immutable full "
                    "publication. Start a separate script version; never silently "
                    "show an outdated finished MP4."
                )
        return
    script = target / (target.name + ".txt")
    if not script.is_file() or script.is_symlink():
        raise PublicationError(f"Managed script missing: {script}")
    if not work.is_dir() or work.is_symlink():
        return
    report_file = work / "run_report.json"
    if report_file.is_symlink():
        raise PublicationError("Linked internal run report")
    report = _json(work, "run_report.json") if report_file.is_file() else None
    stages = (
        sync_managed_media(work, target, script.stem, report)
        if report is not None else {}
    )
    staging = target / (".outputs-" + uuid4().hex + ".partial")
    staging.mkdir()
    try:
        for stage in ("B1.1", "B1.2", "B2", "B2.1", "B3", "CTA"):
            folder = work / stage
            if folder.exists():
                _copy_tree(folder, staging / stage)
        # V bots retain the old private checkpoint layout for safe recovery.
        # Publish the same user-facing stage layout as B1.1/B1.2/B2.
        for stage in ("V1", "V2", "V3", "V4"):
            canonical = work / stage
            legacy = work / "voice_direction" / stage
            if canonical.exists() and legacy.exists():
                raise PublicationError(f"Ambiguous {stage} voice checkpoint roots")
            source = canonical if canonical.exists() else legacy
            if source.exists():
                _copy_tree(source, staging / stage)
        for name in ("visual_plan.json", "avatar_voice.json"):
            source = work / name
            if source.is_symlink():
                raise PublicationError(f"Linked checkpoint: {source}")
            if source.is_file():
                shutil.copyfile(source, staging / name, follow_symlinks=False)
        if report is not None:
            (staging / "run_report.json").write_bytes(
                public_report(report, stages, script.stem)
            )
        if not any(staging.iterdir()):
            staging.rmdir()
            return
        current = target / "outputs"
        _replace_partial_outputs(staging, current)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise


def _expected_managed(
    work: Path, script: Path, target: Path
) -> tuple[dict[str, object], dict[str, tuple[Path | bytes, str]]]:
    if target.resolve() != script.parent.resolve() or target.name != script.stem:
        raise PublicationError("The source script must live directly inside its run folder")
    report = _json(work, "run_report.json")
    avatar_name, entries = _collect(work, script, report)
    if target.parent.name != avatar_name:
        raise PublicationError("The selected avatar differs from the saved visual plan")
    mapped: dict[str, tuple[Path | bytes, str]] = {}
    for name, value in entries.items():
        if name.startswith("Guion/"):
            continue  # The original source has already been moved here.
        if name.startswith("Bots/"):
            mapped["outputs/" + name[len("Bots/"):]] = value
        elif name.startswith("Media/imagenes/"):
            mapped["media/images/" + name[len("Media/imagenes/"):]] = value
        elif name.startswith("Media/audios/"):
            mapped["media/audio/" + name[len("Media/audios/"):]] = value
        else:
            mapped[name] = value
    # The human report uses paths relative to the visible script folder.
    stages = {"audio": "completed", "images": "completed", "video": "completed"}
    if isinstance(report["run"].get("video_preview"), dict) and (
        report["run"]["video_preview"].get("status") == "completed"
    ):
        stages["preview"] = "completed"
    visible = public_report(report, stages, script.stem)
    mapped["outputs/run_report.json"] = (
        visible, hashlib.sha256(visible).hexdigest()
    )
    # The original script is part of the verified snapshot, even when already moved.
    mapped[script.name] = (script, _digest(script))
    files = {name: sha for name, (_source, sha) in sorted(mapped.items())}
    fingerprint = hashlib.sha256(
        json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest: dict[str, object] = {
        "schema_version": "managed-b-pipeline-v1",
        "script_sha256": report["run"]["script_sha256"],
        "fingerprint": fingerprint,
        "files": files,
    }
    return manifest, mapped


def _verify(target: Path, manifest: dict[str, object]) -> None:
    for name, sha in manifest["files"].items():
        item = target / name
        if (
            item.is_symlink() or not item.is_file()
            or not item.resolve().is_relative_to(target.resolve())
            or _digest(item) != sha
        ):
            raise PublicationError(f"Published file changed or missing: {item}")


def publish_managed_run(work: Path, script: Path, target: Path) -> Path:
    """Complete data/<avatar>/<script>/ without touching recovery work.

    One script has one output folder; a different completed result must not
    silently overwrite it. Renaming the manifest is the completion marker.
    """
    _check_dir(target)
    _check_dir(target.parent)
    if not target.is_dir() or target.is_symlink():
        raise PublicationError(f"Managed run folder missing: {target}")
    expected, entries = _expected_managed(work, script, target)
    published = target / "manifest.json"
    if published.is_symlink():
        raise PublicationError(f"Linked publication marker: {published}")
    if published.exists():
        existing = json.loads(published.read_text(encoding="utf-8"))
        if existing != expected:
            raise PublicationError(
                "This script folder already has a different completed generation"
            )
        _verify(target, expected)
        return target

    # Media can already exist from independent successful stage exports.
    # Verify existing assets; never discard audio or regenerate paid work.
    sync_managed_outputs(work, target)
    expected, entries = _expected_managed(work, script, target)
    staging = target / (".publish-" + uuid4().hex + ".partial")
    staging.mkdir()
    try:
        for name, (source, sha) in sorted(entries.items()):
            destination = target / name
            if destination.is_symlink() or destination.is_junction():
                raise PublicationError(f"Linked managed destination: {destination}")
            if destination.exists():
                if not destination.is_file() or _digest(destination) != sha:
                    raise PublicationError(
                        f"Existing managed asset differs from source: {destination}"
                    )
                continue
            staged = staging / name
            staged.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(source, bytes):
                staged.write_bytes(source)
            else:
                shutil.copyfile(source, staged, follow_symlinks=False)
            if _digest(staged) != sha:
                raise PublicationError(f"Corrupt full publication copy: {source}")
        for name in expected["files"]:
            staged = staging / name
            if not staged.exists():
                continue
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise PublicationError(f"Concurrent publication conflict: {destination}")
            staged.rename(destination)
        _verify(target, expected)
        marker_temp = target / (".manifest-" + uuid4().hex + ".partial")
        try:
            marker_temp.write_text(
                json.dumps(expected, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if published.exists() or published.is_symlink():
                raise PublicationError("Concurrent completed publication conflict")
            marker_temp.rename(published)
        finally:
            marker_temp.unlink(missing_ok=True)
    finally:
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
    return target
