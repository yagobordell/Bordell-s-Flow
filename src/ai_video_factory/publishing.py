"""Publish a finished B-pipeline run without moving its durable work files.

The working output remains untouched for checkpoint and paid-job recovery.
A published result is an immutable, separately verified snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_video_factory.providers.openai_batch_images import described_beats


class PublicationError(RuntimeError):
    """A source or published snapshot is unsafe, incomplete or inconsistent."""


_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}\Z")


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _safe_file(work: Path, name: object, digest: object | None = None) -> Path:
    if not isinstance(name, str) or not name:
        raise PublicationError("Missing publication asset path")
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or rel == Path("."):
        raise PublicationError(f"Unsafe publication asset path: {name}")
    source = work / rel
    if (
        source.is_symlink() or not source.is_file()
        or not source.resolve().is_relative_to(work.resolve())
    ):
        raise PublicationError(f"Missing, linked or escaping publication asset: {source}")
    if digest is not None and (not isinstance(digest, str) or _digest(source) != digest):
        raise PublicationError(f"Changed publication asset: {source}")
    return source


def _json(work: Path, name: str) -> dict[str, Any]:
    source = _safe_file(work, name)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PublicationError(f"Invalid publication metadata: {source}")
    return value


def _publication_root(root: Path, work: Path, script: Path) -> Path:
    resolved = root.resolve()
    cwd = Path.cwd().resolve()
    working = work.resolve()
    if (
        resolved == cwd or resolved in cwd.parents
        or resolved == working or resolved.is_relative_to(working)
        or working.is_relative_to(resolved)
        or script.resolve().is_relative_to(resolved)
    ):
        raise PublicationError("Results root must be separate from the source and working output")
    # Never trust a linked component of the publication destination.
    for candidate in (root, *root.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise PublicationError(f"Linked results directory: {candidate}")
    return root


def _collect(
    work: Path, script: Path, report: dict[str, Any]
) -> tuple[str, dict[str, tuple[Path | bytes, str]]]:
    run = report.get("run")
    if not isinstance(run, dict) or run.get("status") != "completed":
        raise PublicationError("The B-pipeline run has not completed")
    if script.is_symlink() or not script.is_file():
        raise PublicationError("The original script must remain an ordinary file")
    source_sha = _digest(script)
    if source_sha != run.get("script_sha256"):
        raise PublicationError("The source script differs from the completed run")
    video = run.get("video_generation")
    audio = run.get("audio_generation")
    if (
        not isinstance(video, dict) or video.get("status") != "completed"
        or not isinstance(audio, dict) or audio.get("status") != "completed"
    ):
        raise PublicationError("Publication requires the actual finished LTX video and narration")
    plan = _json(work, "visual_plan.json")
    images = _json(work, "images/manifest.json")
    if images.get("status") != "completed":
        raise PublicationError("Publication requires verified completed image generation")
    final_assets = (
        images.get("final_assets", [])
        if plan.get("visual_style") == "profesional" else []
    )
    if plan.get("visual_style") == "profesional":
        if images.get("finishing_status") != "completed":
            raise PublicationError("Publication requires finished professional image sources")
        expected = {
            (item["block_id"], item.get("asset_id", item["beat_id"]))
            for item in described_beats(plan)
        }
        keys = {
            (entry.get("block_id"), entry.get("asset_id", entry.get("beat_id")))
            for entry in final_assets
            if isinstance(entry, dict)
        } if isinstance(final_assets, list) else set()
        if (
            not isinstance(final_assets, list)
            or len(final_assets) != len(expected)
            or keys != expected
        ):
            raise PublicationError("Finished professional image assets differ from B2")
    avatar = plan.get("avatar")
    if not isinstance(avatar, dict):
        raise PublicationError("The visual plan has no saved avatar")
    filename = avatar.get("filename")
    if (
        not isinstance(filename, str) or Path(filename).name != filename
        or not filename.lower().endswith(".png")
    ):
        raise PublicationError("The visual plan has an invalid avatar name")

    entries: dict[str, tuple[Path | bytes, str]] = {}

    def add(destination: str, name: str, sha: object | None = None) -> None:
        if destination in entries:
            raise PublicationError(f"Duplicate published asset: {destination}")
        if (destination.startswith("Media/") or destination.endswith(".mp4")) and (
            not isinstance(sha, str) or re.fullmatch("[0-9a-f]{64}", sha) is None
        ):
            raise PublicationError(f"Missing verified SHA-256 for {destination}")
        original = _safe_file(work, name, sha)
        entries[destination] = (original, _digest(original))

    # The exact script bytes are the original input, not a rewritten bot transcript.
    entries["Guion/" + script.name] = (script, source_sha)
    for stage in ("B1.1", "B1.2", "B2"):
        folder = work / stage
        if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
            raise PublicationError(f"Missing or linked bot stage: {stage}")
        for item in sorted(folder.rglob("*")):
            if item.is_symlink() or item.is_junction():
                raise PublicationError(f"Linked bot checkpoint: {item}")
            if item.is_file():
                rel = item.relative_to(work).as_posix()
                add("Bots/" + rel, rel)
    # This directory exists only for Professional OpenAI runs. Archive all
    # accepted and rejected phase artifacts alongside the legacy B-stage
    # outputs in completed publications; Casero is unaffected.
    agent_output = work / "agent_openai"
    if agent_output.exists():
        if not agent_output.is_dir() or agent_output.is_symlink() or agent_output.is_junction():
            raise PublicationError("Unsafe Professional OpenAI checkpoint directory")
        for item in sorted(agent_output.rglob("*")):
            if item.is_symlink() or item.is_junction():
                raise PublicationError("Linked Professional OpenAI checkpoint")
            if item.is_file():
                rel = item.relative_to(work).as_posix()
                add("Bots/" + rel, rel)
    for stage in ("V1", "V2", "V3", "V4"):
        canonical = work / stage
        legacy = work / "voice_direction" / stage
        if canonical.exists() and legacy.exists():
            raise PublicationError(f"Ambiguous {stage} voice checkpoint roots")
        folder = canonical if canonical.exists() else legacy
        if folder.exists():
            if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
                raise PublicationError(f"Linked voice checkpoint root: {stage}")
            for item in sorted(folder.rglob("*")):
                if item.is_symlink() or item.is_junction():
                    raise PublicationError(f"Linked voice checkpoint: {item}")
                if item.is_file():
                    rel = item.relative_to(folder).as_posix()
                    add(f"Bots/{stage}/{rel}", item.relative_to(work).as_posix())
    cta_folder = work / "CTA"
    if cta_folder.exists():
        if cta_folder.is_symlink() or not cta_folder.is_dir():
            raise PublicationError("Unsafe casero CTA placement directory")
        for item in sorted(cta_folder.rglob("*")):
            if item.is_symlink():
                raise PublicationError("Linked CTA placement artifact")
            if item.is_file():
                rel = item.relative_to(work).as_posix()
                add("Bots/" + rel, rel)
    for name in ("visual_plan.json", "avatar_voice.json"):
        add("Bots/" + name, name)
    # Strip the mutable publication pointer: snapshots stay identical on safe retries.
    snapshot = json.loads(json.dumps(report, ensure_ascii=False))
    snapshot["run"].pop("publication", None)
    snapshot_bytes = (
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    entries["Bots/run_report.json"] = (
        snapshot_bytes, hashlib.sha256(snapshot_bytes).hexdigest()
    )
    add("Media/imagenes/avatar.png", avatar.get("file"), avatar.get("sha256"))
    qr = avatar.get("qr")
    if isinstance(qr, dict):
        add("Media/imagenes/qr.png", qr.get("file"), qr.get("sha256"))

    image_assets: dict[str, str] = {}
    # Professional beat PNGs are replaced in place: never package the source
    # metadata alongside a different final SHA for the very same file.
    if plan.get("visual_style") == "profesional":
        selected_images = [
            *(item for item in images.get("items", []) if item.get("grid") is not None),
            *final_assets,
        ]
    else:
        selected_images = [*images.get("items", []), *images.get("panel_assets", [])]
    for item in selected_images:
        if not isinstance(item, dict):
            raise PublicationError("Invalid image manifest item")
        name, sha = item.get("file"), item.get("sha256")
        if not isinstance(name, str) or not name.startswith("images/"):
            raise PublicationError("Generated image is outside the image directory")
        if name in image_assets and image_assets[name] != sha:
            raise PublicationError("Conflicting generated image hashes")
        image_assets[name] = sha
    for name, sha in sorted(image_assets.items()):
        add("Media/imagenes/" + name[len("images/"):], name, sha)

    add("Media/audios/narracion.wav", audio.get("file"), audio.get("sha256"))
    for block in audio.get("blocks", []):
        if not isinstance(block, dict) or not isinstance(block.get("block_id"), int):
            raise PublicationError("Invalid audio block record")
        filename = block.get("file")
        if not isinstance(filename, str) or Path(filename).suffix.lower() not in {
            ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".webm"
        }:
            raise PublicationError("Invalid audio block file")
        add(
            f"Media/audios/bloques/bloque_{block['block_id']}{Path(filename).suffix.lower()}",
            filename, block.get("sha256"),
        )
    add(script.stem + ".mp4", video.get("file"), video.get("sha256"))
    return Path(avatar["filename"]).stem, entries


def _verify_existing(destination: Path, expected: dict[str, Any]) -> None:
    manifest_path = destination / "manifest.json"
    if destination.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise PublicationError(f"Published snapshot is missing or linked: {destination}")
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if recorded != expected:
        raise PublicationError("An existing publication differs from the saved run")
    for name, sha in expected["files"].items():
        file = destination / name
        if (
            file.is_symlink() or not file.is_file()
            or not file.resolve().is_relative_to(destination.resolve())
            or _digest(file) != sha
        ):
            raise PublicationError(f"Published asset is missing or modified: {file}")


def publish_completed_run(
    work: Path, script: Path, results_root: Path = Path("data/results")
) -> Path:
    """Snapshot a verified LTX run; identical retries are idempotent.

    New or changed generated media gets a new ID. Never modify an existing result.
    """
    report = _json(work, "run_report.json")
    avatar_name, entries = _collect(work, script, report)
    root = _publication_root(results_root, work, script)
    parent = root / avatar_name / script.stem
    if any(item.is_symlink() or item.is_junction() for item in (root, parent.parent, parent)):
        raise PublicationError("Linked publication directory")
    files = {name: sha for name, (_source, sha) in sorted(entries.items())}
    fingerprint = hashlib.sha256(
        json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    previous = report["run"].get("publication")
    prior_id = previous.get("generation_id") if isinstance(previous, dict) else None
    if prior_id is not None and not _ID.fullmatch(prior_id):
        raise PublicationError("Invalid prior publication identifier")
    if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
        target = parent / prior_id
        expected = {
            "schema_version": "b-pipeline-publication-v1",
            "generation_id": prior_id,
            "script_sha256": report["run"]["script_sha256"],
            "fingerprint": fingerprint,
            "files": files,
        }
        _verify_existing(target, expected)
        return target

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    target = parent / run_id
    staging = parent / ("." + run_id + ".partial")
    if target.exists() or target.is_symlink() or staging.exists() or staging.is_symlink():
        raise PublicationError("Publication destination already exists; refusing to overwrite")
    parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    expected = {
        "schema_version": "b-pipeline-publication-v1",
        "generation_id": run_id,
        "script_sha256": report["run"]["script_sha256"],
        "fingerprint": fingerprint,
        "files": files,
    }
    try:
        for name, (original, sha) in sorted(entries.items()):
            dest = staging / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(original, bytes):
                dest.write_bytes(original)
            else:
                shutil.copyfile(original, dest, follow_symlinks=False)
            if _digest(dest) != sha:
                raise PublicationError(f"Copy changed during publication: {original}")
        (staging / "manifest.json").write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _verify_existing(staging, expected)
        if target.exists() or target.is_symlink():
            raise PublicationError("Concurrent publication collision")
        staging.rename(target)
    except Exception:
        # This unique staging directory belongs to this invocation only.
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise

    report["run"]["publication"] = {
        "status": "completed",
        "generation_id": run_id,
        "fingerprint": fingerprint,
        "path": target.as_posix(),
    }
    report_path = work / "run_report.json"
    temporary = work / "run_report.publication.tmp"
    if temporary.is_symlink():
        raise PublicationError("Linked temporary run report")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)
    return target
