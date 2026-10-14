"""Immutable, validated human-facing results without touching resumable work files."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

from ai_video_factory.publishing import PublicationError, publish_completed_run
from scripts.pipeline.run_b_pipeline import _publish_if_ready, main


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(work: Path, relative: str, data: bytes) -> str:
    file = work / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)
    return _sha(data)


def _json(work: Path, relative: str, obj: object) -> None:
    _put(work, relative, (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


@pytest.fixture
def completed(tmp_path: Path) -> tuple[Path, Path, Path]:
    work = tmp_path / "output" / "tutorial"
    work.mkdir(parents=True)
    script = tmp_path / "input" / "tutorial.txt"
    script.parent.mkdir()
    script.write_bytes(b"Hola, mundo!")
    for stage in ("B1.1", "B1.2", "B2"):
        _json(work, f"{stage}/block_1/output.json", {"stage": stage})
    avatar_sha = _put(work, "avatar.png", b"avatar PNG bytes")
    qr_sha = _put(work, "qr.png", b"QR PNG bytes")
    _json(work, "avatar_voice.json", {"voice_id": "example"})
    _json(work, "visual_plan.json", {
        "avatar": {
            "filename": "Jorge.png",
            "file": "avatar.png",
            "sha256": avatar_sha,
            "qr": {"file": "qr.png", "sha256": qr_sha},
        },
    })
    image_sha = _put(work, "images/block_1/1B.png", b"image")
    _json(work, "images/manifest.json", {
        "status": "completed",
        "items": [{"file": "images/block_1/1B.png", "sha256": image_sha}],
        "panel_assets": [],
    })
    block_sha = _put(work, "audio/runs/a/blocks/block_1/voice.mp3", b"block audio")
    audio_sha = _put(work, "audio/runs/a/narration.wav", b"master audio")
    video_sha = _put(work, "video/runs/v1/final.mp4", b"complete video")
    _json(work, "run_report.json", {
        "run": {
            "status": "completed",
            "script_sha256": _sha(script.read_bytes()),
            "audio_generation": {
                "status": "completed",
                "file": "audio/runs/a/narration.wav",
                "sha256": audio_sha,
                "blocks": [{
                    "block_id": 1,
                    "file": "audio/runs/a/blocks/block_1/voice.mp3",
                    "sha256": block_sha,
                }],
            },
            "video_generation": {
                "status": "completed",
                "file": "video/runs/v1/final.mp4",
                "sha256": video_sha,
            },
        },
    })
    return work, script, tmp_path / "results"


def test_publishes_expected_layout_and_keeps_working_files(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    published = publish_completed_run(work, script, root)
    assert published.parent == root / "Jorge" / "tutorial"
    assert (published / "tutorial.mp4").read_bytes() == b"complete video"
    assert (published / "Guion" / "tutorial.txt").read_bytes() == b"Hola, mundo!"
    assert (published / "Bots" / "B1.1" / "block_1" / "output.json").is_file()
    assert (published / "Bots" / "B1.2" / "block_1" / "output.json").is_file()
    assert (published / "Bots" / "B2" / "block_1" / "output.json").is_file()
    assert (published / "Bots" / "visual_plan.json").is_file()
    assert (published / "Media" / "imagenes" / "avatar.png").is_file()
    assert (published / "Media" / "imagenes" / "qr.png").is_file()
    assert (published / "Media" / "imagenes" / "block_1" / "1B.png").is_file()
    assert (published / "Media" / "audios" / "narracion.wav").is_file()
    assert (published / "Media" / "audios" / "bloques" / "bloque_1.mp3").is_file()
    assert (work / "video" / "runs" / "v1" / "final.mp4").read_bytes() == b"complete video"
    assert (work / "audio" / "runs" / "a" / "narration.wav").is_file()
    assert (work / "images" / "manifest.json").is_file()
    saved = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    assert saved["run"]["publication"]["path"] == published.as_posix()
    public_report = json.loads(
        (published / "Bots" / "run_report.json").read_text(encoding="utf-8")
    )
    assert "publication" not in public_report["run"]



def test_imported_professional_run_publishes_without_b11_or_b12(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    import shutil

    shutil.rmtree(work / "B1.1")
    shutil.rmtree(work / "B1.2")
    _json(work, "B2/merged_output.json", {"pipeline_stage": "B2"})
    plan = json.loads((work / "visual_plan.json").read_text(encoding="utf-8"))
    plan.update({"visual_style": "profesional", "blocks": []})
    _json(work, "visual_plan.json", plan)
    images = json.loads((work / "images/manifest.json").read_text(encoding="utf-8"))
    images.update({"finishing_status": "completed", "final_assets": []})
    _json(work, "images/manifest.json", images)
    report = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    report["run"].update({
        "input_kind": "professional_merged_b2",
        "visual_style": "profesional",
    })
    _json(work, "run_report.json", report)

    published = publish_completed_run(work, script, root)
    assert (published / "Bots" / "B2" / "merged_output.json").is_file()
    assert not (published / "Bots" / "B1.1").exists()
    assert not (published / "Bots" / "B1.2").exists()


def test_professional_subshots_publish_by_asset_id(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    plan = json.loads((work / "visual_plan.json").read_text(encoding="utf-8"))
    plan["visual_style"] = "profesional"
    plan["blocks"] = [{
        "block_id": 1,
        "beats": [{
            "beat_id": "1B",
            "text": "Two related visual moments.",
            "visual_type": "media_image",
            "prompt": "First moment.",
            "reference": False,
            "audio": False,
            "visual_subshots": [
                {
                    "asset_id": "1B__1",
                    "subshot_index": 1,
                    "subshot_count": 2,
                    "generation": "independent",
                    "prompt": "First moment.",
                },
                {
                    "asset_id": "1B__2",
                    "subshot_index": 2,
                    "subshot_count": 2,
                    "generation": "independent",
                    "prompt": "Second moment.",
                },
            ],
        }],
    }]
    _json(work, "visual_plan.json", plan)

    first_sha = _put(work, "images/block_1/1B__1.png", b"finished one")
    second_sha = _put(work, "images/block_1/1B__2.png", b"finished two")
    final_assets = [
        {
            "block_id": 1, "beat_id": "1B", "asset_id": "1B__1",
            "file": "images/block_1/1B__1.png", "sha256": first_sha,
        },
        {
            "block_id": 1, "beat_id": "1B", "asset_id": "1B__2",
            "file": "images/block_1/1B__2.png", "sha256": second_sha,
        },
    ]
    _json(work, "images/manifest.json", {
        "status": "completed",
        "finishing_status": "completed",
        "items": [],
        "panel_assets": [],
        "final_assets": final_assets,
    })

    published = publish_completed_run(work, script, root)

    assert (
        published / "Media" / "imagenes" / "block_1" / "1B__1.png"
    ).read_bytes() == b"finished one"
    assert (
        published / "Media" / "imagenes" / "block_1" / "1B__2.png"
    ).read_bytes() == b"finished two"



def test_retry_reuses_and_verifies_same_immutable_snapshot(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    first = publish_completed_run(work, script, root)
    second = publish_completed_run(work, script, root)
    assert first == second
    assert len(list(first.parent.iterdir())) == 1
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "b-pipeline-publication-v1"
    assert manifest["files"]["tutorial.mp4"] == _sha(b"complete video")


def test_changed_source_creates_new_snapshot_without_replacing_old(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    first = publish_completed_run(work, script, root)
    updated_video = b"another completed video"
    new_sha = _put(work, "video/runs/v1/final.mp4", updated_video)
    report = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    report["run"]["video_generation"]["sha256"] = new_sha
    _json(work, "run_report.json", report)
    second = publish_completed_run(work, script, root)
    assert first != second
    assert (first / "tutorial.mp4").read_bytes() == b"complete video"
    assert (second / "tutorial.mp4").read_bytes() == updated_video
    assert len(list(first.parent.iterdir())) == 2


def test_modified_published_asset_fails_closed(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    first = publish_completed_run(work, script, root)
    (first / "tutorial.mp4").write_bytes(b"tampered")
    with pytest.raises(PublicationError, match="missing or modified"):
        publish_completed_run(work, script, root)


def test_bad_source_sha_is_never_published(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    (work / "images" / "block_1" / "1B.png").write_bytes(b"tampered")
    with pytest.raises(PublicationError, match="Changed publication asset"):
        publish_completed_run(work, script, root)
    assert not root.exists()


def test_linked_bot_checkpoint_is_not_copied(
    completed: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    work, script, root = completed
    linked = work / "B2" / "linked.json"
    try:
        linked.symlink_to(tmp_path / "outside.json")
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not available on this filesystem")
    with pytest.raises(PublicationError, match="Linked bot checkpoint"):
        publish_completed_run(work, script, root)


def test_publication_root_must_be_separate(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, _root = completed
    with pytest.raises(PublicationError, match="Results root must be separate"):
        publish_completed_run(work, script, work / "results")


def test_deferred_video_is_not_published_by_normal_runner(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    report = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    report["run"]["video_generation"] = {"status": "deferred"}
    _json(work, "run_report.json", report)
    assert _publish_if_ready(work, script, root) is None
    assert not root.exists()


def test_publisher_rejects_deferred_video(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    report = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    report["run"]["video_generation"] = {"status": "deferred"}
    _json(work, "run_report.json", report)
    with pytest.raises(PublicationError, match="actual finished LTX video"):
        publish_completed_run(work, script, root)
    assert not root.exists()


def test_publish_only_cli_uses_saved_run_without_provider_calls(
    completed: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, script, root = completed

    def no_api(*args: object, **kwargs: object) -> None:
        raise AssertionError("Publishing must never construct a paid provider")

    monkeypatch.setattr("scripts.pipeline.run_b_pipeline.OpenAIProvider", no_api)
    monkeypatch.setattr("scripts.pipeline.run_b_pipeline.AI33SpeechClient", no_api)
    monkeypatch.setattr(sys, "argv", [
        "run_b_pipeline.py", str(script), "--publish-only",
        "--output", str(work.parent),
        "--results-root", str(root),
        "--avatars-dir", str(tmp_path / "avatars"),
    ])
    asyncio.run(main())
    assert len(list((root / "Jorge" / "tutorial").iterdir())) == 1
    assert len(list((root / "Jorge" / "tutorial").glob("*/tutorial.mp4"))) == 1


def test_manifest_missing_authoritative_media_sha_is_rejected(
    completed: tuple[Path, Path, Path],
) -> None:
    work, script, root = completed
    report = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    report["run"]["video_generation"].pop("sha256")
    _json(work, "run_report.json", report)
    with pytest.raises(PublicationError, match="Missing verified SHA-256"):
        publish_completed_run(work, script, root)
    assert not root.exists()
