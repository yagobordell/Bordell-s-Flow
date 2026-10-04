"""Verify and expose each saved media stage independently of final LTX completion.

Internal paid-job state is never moved. The managed script folder receives
stage-level verified snapshots as soon as their files are available.
"""
from __future__ import annotations

import json
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_video_factory.publishing import PublicationError, _digest, _json, _safe_file

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".webm"}


def _source(work: Path, relative: object, digest: object) -> tuple[Path, str]:
    if not isinstance(digest, str) or _SHA.fullmatch(digest) is None:
        raise PublicationError(f"Missing authoritative asset SHA-256: {relative}")
    return _safe_file(work, relative, digest), digest


def _artifact(artifact: object) -> dict[str, Any] | None:
    if not isinstance(artifact, dict) or artifact.get("status") != "completed":
        return None
    return artifact


def _audio_sources(
    work: Path, run: dict[str, Any]
) -> dict[str, tuple[Path, str]] | None:
    audio = _artifact(run.get("audio_generation"))
    if audio is None:
        return None
    entries = {"narracion.wav": _source(work, audio.get("file"), audio.get("sha256"))}
    blocks = audio.get("blocks")
    if not isinstance(blocks, list) or not blocks or (
        isinstance(audio.get("block_count"), int)
        and len(blocks) != audio["block_count"]
    ):
        raise PublicationError("Completed audio has no complete block list")
    for block in blocks:
        if (
            not isinstance(block, dict)
            or type(block.get("block_id")) is not int
            or block["block_id"] < 1
            or not isinstance(block.get("file"), str)
        ):
            raise PublicationError("Completed audio has invalid block metadata")
        suffix = Path(block["file"]).suffix.lower()
        if suffix not in _AUDIO_EXTENSIONS:
            raise PublicationError("Completed audio has an invalid block format")
        name = f"bloques/bloque_{block['block_id']}{suffix}"
        if name in entries:
            raise PublicationError(f"Duplicate completed audio block: {name}")
        entries[name] = _source(work, block["file"], block.get("sha256"))
    return entries


def _image_sources(
    work: Path, run: dict[str, Any]
) -> tuple[str, dict[str, tuple[Path, str]]] | None:
    file = work / "images" / "manifest.json"
    claim = _artifact(run.get("image_generation"))
    if file.is_symlink():
        raise PublicationError("Linked image manifest")
    if not file.is_file():
        if claim is not None:
            raise PublicationError("Completed image generation is missing its manifest")
        return None
    manifest = _json(work, "images/manifest.json")
    status = manifest.get("status")
    if status not in {"running", "incomplete", "completed"}:
        return None
    if claim is not None and status != "completed":
        raise PublicationError("Completed image report has an incomplete image manifest")
    items = manifest.get("items", [])
    panels = manifest.get("panel_assets", [])
    if not isinstance(items, list) or not isinstance(panels, list):
        raise PublicationError("Invalid generated-image item list")
    if status == "completed" and (
        len(items) != manifest.get("total_generation_jobs", len(items))
        or (
            isinstance(run.get("image_generation"), dict)
            and run["image_generation"].get("status") == "completed"
            and run["image_generation"].get("count") != len(items)
        )
        or (
            isinstance(run.get("image_generation"), dict)
            and "cropped_panel_count" in run["image_generation"]
            and run["image_generation"]["cropped_panel_count"] != len(panels)
        )
    ):
        raise PublicationError("Completed image manifest does not match its generated items")
    final = manifest.get("final_assets", [])
    if not isinstance(final, list):
        raise PublicationError("Invalid professional finished-image list")
    visual_for_finishing = work / "visual_plan.json"
    if visual_for_finishing.is_file() and not visual_for_finishing.is_symlink():
        selected_style = _json(work, "visual_plan.json").get("visual_style")
    else:
        selected_style = None
    if selected_style == "profesional":
        if claim is not None and manifest.get("finishing_status") != "completed":
            raise PublicationError("Professional images are not finished")
        if manifest.get("finishing_status") == "completed":
            visual_data = _json(work, "visual_plan.json")
            expected = {
                (block["block_id"], beat["beat_id"])
                for block in visual_data["blocks"] for beat in block["beats"]
                if beat.get("prompt") is not None
            }
            keys = {(item.get("block_id"), item.get("beat_id")) for item in final}
            if keys != expected or len(final) != len(expected):
                raise PublicationError("Professional finished images do not match B2")
        else:
            final = []
    else:
        final = []
    entries: dict[str, tuple[Path, str]] = {}
    if selected_style == "profesional" and manifest.get("finishing_status") == "completed":
        selected_images = [
            *(item for item in items if item.get("grid") is not None),
            *final,
        ]
    else:
        # A partially finished run may contain a mix of source and finished
        # beat PNGs. Prefer finished records for paths already replaced.
        finished_paths = {item.get("file") for item in final}
        selected_images = [
            *(item for item in [*items, *panels] if item.get("file") not in finished_paths),
            *final,
        ]
    for item in selected_images:
        if not isinstance(item, dict):
            raise PublicationError("Invalid saved image record")
        name = item.get("file")
        if not isinstance(name, str) or not name.startswith("images/"):
            raise PublicationError("Generated image escaped the image directory")
        destination = name[len("images/"):]
        if destination in entries:
            raise PublicationError(f"Duplicate image publication: {destination}")
        entries[destination] = _source(work, name, item.get("sha256"))
    visual = work / "visual_plan.json"
    if status == "completed":
        if not visual.is_file() or visual.is_symlink():
            raise PublicationError("Completed images require the saved visual plan")
        plan = _json(work, "visual_plan.json")
        avatar = plan.get("avatar")
        if not isinstance(avatar, dict):
            raise PublicationError("Completed visual plan is missing the avatar")
        entries["avatar.png"] = _source(
            work, avatar.get("file"), avatar.get("sha256")
        )
        qr = avatar.get("qr")
        if isinstance(qr, dict):
            entries["qr.png"] = _source(work, qr.get("file"), qr.get("sha256"))
    published_status = (
        "completed" if status == "completed" and claim is not None else "incomplete"
    )
    return published_status, entries


def _preview_sources(
    work: Path, run: dict[str, Any]
) -> dict[str, tuple[Path, str]] | None:
    preview = _artifact(run.get("video_preview"))
    if preview is None:
        return None
    manifest = _json(work, "preview/manifest.json")
    audio = _artifact(run.get("audio_generation"))
    if (
        manifest.get("status") != "completed"
        or manifest.get("file") != preview.get("file")
        or manifest.get("sha256") != preview.get("sha256")
        or audio is None or manifest.get("audio_file") != audio.get("file")
    ):
        raise PublicationError("Completed preview is stale or differs from its audio")
    video = _source(work, preview.get("file"), preview.get("sha256"))
    timeline_file = _safe_file(work, manifest.get("timeline_file"))
    return {
        "preview.mp4": video,
        "timeline.json": (timeline_file, _digest(timeline_file)),
    }


def _final_video(
    work: Path, run: dict[str, Any], stages: dict[str, str],
) -> tuple[Path, str] | None:
    video = _artifact(run.get("video_generation"))
    if video is None:
        return None
    if stages.get("audio") != "completed" or stages.get("images") != "completed":
        raise PublicationError("Completed final video requires verified image and audio stages")
    source = _source(work, video.get("file"), video.get("sha256"))
    # Do not publish a stale mux from a previous narration regeneration.
    audio = run["audio_generation"]
    path = Path(video["file"])
    manifest = path.parent / "manifest.json"
    state = _json(work, manifest.as_posix())
    if (
        state.get("status") != "completed"
        or state.get("file") != video.get("file")
        or state.get("sha256") != video.get("sha256")
        or state.get("master_audio_sha256") != audio["sha256"]
    ):
        raise PublicationError("Completed final video does not match its LTX/audio manifest")
    return source


def _stage_manifest(
    stage: str, entries: dict[str, tuple[Path, str]], status: str
) -> dict[str, Any]:
    return {
        "schema_version": "managed-stage-v1",
        "stage": stage,
        "status": status,
        "files": {name: digest for name, (_, digest) in sorted(entries.items())},
    }


def _verify_stage(directory: Path, recorded: dict[str, Any]) -> None:
    files = recorded.get("files")
    if not isinstance(files, dict) or not isinstance(recorded.get("status"), str):
        raise PublicationError(f"Invalid published stage manifest: {directory}")
    for name, sha in files.items():
        relative = Path(name)
        path = directory / relative
        if (
            not isinstance(name, str) or relative.is_absolute()
            or ".." in relative.parts or path.is_symlink() or not path.is_file()
            or not path.resolve().is_relative_to(directory.resolve())
            or _digest(path) != sha
        ):
            raise PublicationError(f"Published stage file is missing or modified: {path}")


def _publish_stage(
    target: Path, stage: str, entries: dict[str, tuple[Path, str]], status: str
) -> None:
    media = target / "media"
    if media.is_symlink() or media.is_junction():
        raise PublicationError("Linked managed media directory")
    media.mkdir(exist_ok=True)
    current = media / stage
    marker = current / "manifest.json"
    manifest = _stage_manifest(stage, entries, status)
    if current.is_symlink() or current.is_junction():
        raise PublicationError(f"Linked existing stage: {current}")
    if current.exists():
        if not marker.is_file() or marker.is_symlink():
            raise PublicationError(f"Existing media stage has no verified manifest: {current}")
        previous = json.loads(marker.read_text(encoding="utf-8"))
        _verify_stage(current, previous)
        if previous == manifest:
            return
    staging = target / f".{stage}-{uuid4().hex}.partial"
    backup = target / f".{stage}-{uuid4().hex}.backup"
    staging.mkdir()
    renamed_previous = False
    try:
        for relative, (source, digest) in sorted(entries.items()):
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
            if _digest(destination) != digest:
                raise PublicationError(f"Stage copy failed SHA-256 verification: {source}")
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _verify_stage(staging, manifest)
        if current.exists():
            current.rename(backup)
            renamed_previous = True
        try:
            staging.rename(current)
        except Exception:
            if renamed_previous and backup.is_dir():
                backup.rename(current)
            raise
        if renamed_previous:
            shutil.rmtree(backup)
    finally:
        if staging.is_dir():
            shutil.rmtree(staging)


def _publish_final_file(
    target: Path, script_name: str, source: tuple[Path, str]
) -> None:
    name = f"{script_name}.mp4"
    destination = target / name
    asset, sha = source
    if destination.is_symlink():
        raise PublicationError("Linked published final video")
    if destination.exists():
        if not destination.is_file() or _digest(destination) != sha:
            raise PublicationError("Existing published final video differs from the saved run")
        return
    temporary = target / f".{name}-{uuid4().hex}.partial"
    try:
        shutil.copyfile(asset, temporary, follow_symlinks=False)
        if _digest(temporary) != sha:
            raise PublicationError("Final video copy failed SHA-256 verification")
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)


def public_report(
    report: dict[str, Any], stages: dict[str, str], script_name: str
) -> bytes:
    """Human-facing report: paths resolve from the managed script folder, not work."""
    human = deepcopy(report)
    run = human.get("run")
    if not isinstance(run, dict):
        raise PublicationError("Invalid run report")
    run.pop("publication", None)
    # A planning/audio-only execution may say completed internally without
    # having created a video. The visible report describes delivery status.
    run["internal_status"] = run.get("status")
    run["status"] = (
        "completed"
        if all(stages.get(key) == "completed" for key in ("audio", "images", "video"))
        else "partial"
    )
    run["asset_path_base"] = "script_folder"
    run["published_media"] = dict(stages)
    audio = run.get("audio_generation")
    if isinstance(audio, dict) and stages.get("audio") == "completed":
        audio["file"] = "media/audio/narracion.wav"
        for block in audio.get("blocks", []):
            block["file"] = (
                "media/audio/bloques/"
                f"bloque_{block['block_id']}{Path(block['file']).suffix.lower()}"
            )
    images = run.get("image_generation")
    if isinstance(images, dict) and stages.get("images") == "completed":
        images["manifest_file"] = "media/images/manifest.json"
    preview = run.get("video_preview")
    if isinstance(preview, dict) and stages.get("preview") == "completed":
        preview["file"] = "media/preview/preview.mp4"
        preview["timeline_file"] = "media/preview/timeline.json"
    video = run.get("video_generation")
    if isinstance(video, dict) and stages.get("video") == "completed":
        video["file"] = f"{script_name}.mp4"
    # Do not expose an internal relative path as though it existed here.
    for key, stage in (
        ("audio_generation", "audio"), ("video_preview", "preview"),
        ("video_generation", "video"),
    ):
        item = run.get(key)
        if isinstance(item, dict) and item.get("status") == "completed" and (
            stages.get(stage) != "completed"
        ):
            raise PublicationError(f"Cannot expose unverified completed {stage} metadata")
    return (json.dumps(human, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sync_managed_media(
    work: Path, target: Path, script_name: str, report: dict[str, Any]
) -> dict[str, str]:
    """Export verified stages, even if downstream production subsequently fails."""
    run = report.get("run")
    if not isinstance(run, dict):
        raise PublicationError("Saved report has no run metadata")
    statuses: dict[str, str] = {}
    audio = _audio_sources(work, run)
    if audio is not None:
        _publish_stage(target, "audio", audio, "completed")
        statuses["audio"] = "completed"
    images = _image_sources(work, run)
    if images is not None:
        status, sources = images
        _publish_stage(target, "images", sources, status)
        statuses["images"] = status
    preview = _preview_sources(work, run)
    if preview is not None:
        if statuses.get("audio") != "completed" or statuses.get("images") != "completed":
            raise PublicationError("Completed preview requires verified current audio and images")
        _publish_stage(target, "preview", preview, "completed")
        statuses["preview"] = "completed"
    video = _final_video(work, run, statuses)
    if video is not None:
        _publish_final_file(target, script_name, video)
        statuses["video"] = "completed"
    return statuses
