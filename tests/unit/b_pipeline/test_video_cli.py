"""CLI contracts: preview-only run and resumable video from an existing preview."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts.pipeline import run_b_pipeline as runner


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _saved_preview(tmp_path: Path) -> tuple[Path, Path, Path]:
    script = tmp_path / "input" / "test2.txt"
    script.parent.mkdir()
    script.write_text("Texto original del vídeo.", encoding="utf-8")
    root = tmp_path / "output"
    output = root / "test2"
    (output / "B2").mkdir(parents=True)
    (output / "B2" / "merged_output.json").write_text("{}", encoding="utf-8")
    (output / "visual_plan.json").write_text('{"blocks":[]}', encoding="utf-8")
    preview = output / "preview" / "static_preview.mp4"
    preview.parent.mkdir()
    preview.write_bytes(b"verified preview")
    runner._write(output / "run_report.json", {
        "run": {
            "script_sha256": _sha(script),
            "status": "completed",
            "stt_alignment": {"status": "completed"},
            "audio_generation": {"status": "completed", "stt_complete": True},
            "image_generation": {"status": "completed"},
            "video_preview": {
                "status": "completed",
                "file": "preview/static_preview.mp4",
                "sha256": _sha(preview),
            },
            "video_generation": {"status": "skipped"},
        },
        "timings": {"status": "completed"},
        "api_costs": {"run_status": "completed"},
    })
    return script, root, output


@pytest.mark.parametrize(
    ("option", "videos_only", "no_video"),
    [
        ("--no-video", False, True),
        ("--generate-video", True, False),
        ("--videos-only", True, False),
    ],
)
def test_video_cli_flags(
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    videos_only: bool,
    no_video: bool,
) -> None:
    monkeypatch.setattr(sys, "argv", ["run_b_pipeline.py", "--script", "test2", option])
    args = runner.parse_args()
    assert args.videos_only is videos_only
    assert args.no_video is no_video


def test_generate_video_rejects_no_video_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["run_b_pipeline.py", "--generate-video", "--no-video"]
    )
    with pytest.raises(SystemExit):
        runner.parse_args()


def test_generate_video_uses_existing_preview_without_repeating_paid_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script, root, output = _saved_preview(tmp_path)
    saved_script = script.read_bytes()
    saved_plan = (output / "visual_plan.json").read_bytes()
    saved_preview = (output / "preview" / "static_preview.mp4").read_bytes()
    calls: list[tuple[Path, bool]] = []

    async def fake_video(
        destination: Path, report: dict, *, require: bool = False, **_kwargs: object,
    ) -> dict:
        assert report["run"]["video_preview"]["status"] == "completed"
        calls.append((destination, require))
        return {"status": "completed", "file": "video/runs/test/final.mp4"}

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Video resume must not repeat B, image, Fish or preview work")

    monkeypatch.setattr(runner, "_video_if_ready", fake_video)
    # The fake returns no MP4 bytes, digest or image manifest: publication
    # is covered separately with actual saved-file fixtures.
    monkeypatch.setattr(runner, "_publish_if_ready", lambda *_args: None)
    monkeypatch.setattr(runner, "_generate_images", forbidden)
    monkeypatch.setattr(runner, "_generate_audio", forbidden)
    monkeypatch.setattr(runner, "run_b_pipeline", forbidden)
    monkeypatch.setattr(runner, "_preview_if_ready", forbidden)
    asyncio.run(runner._run_videos_only(script, root))

    assert calls == [(output, True)]
    saved = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert saved["run"]["video_generation"]["status"] == "completed"
    assert saved["run"]["video_preview"]["status"] == "completed"
    assert saved["run"]["status"] == "completed"
    assert script.read_bytes() == saved_script
    assert (output / "visual_plan.json").read_bytes() == saved_plan
    assert (output / "preview" / "static_preview.mp4").read_bytes() == saved_preview


@pytest.mark.parametrize("invalid", ["incomplete", "missing", "modified"])
def test_generate_video_rejects_invalid_preview_without_starting_ltx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    script, root, output = _saved_preview(tmp_path)
    preview = output / "preview" / "static_preview.mp4"
    report_path = output / "run_report.json"
    if invalid == "incomplete":
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["run"]["video_preview"]["status"] = "incomplete"
        runner._write(report_path, report)
    elif invalid == "missing":
        preview.unlink()
    else:
        preview.write_bytes(b"changed preview")

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("No GPU requests for an incomplete or corrupt preview")

    monkeypatch.setattr(runner, "_video_if_ready", forbidden)
    with pytest.raises(SystemExit, match="preview"):
        asyncio.run(runner._run_videos_only(script, root))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run"]["video_generation"] == {"status": "skipped"}



def test_saved_completed_audio_is_preflighted_against_resumed_b2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, dict, dict]] = []
    audio = {"status": "completed", "stt_complete": True, "blocks": []}
    run_metadata: dict[str, object] = {"audio_generation": audio}
    plan = {"blocks": [{"block_id": 5, "beats": []}]}

    async def fake_preflight(
        output: Path,
        visual_plan: dict,
        saved_audio: dict,
        *,
        allow_audio_regeneration: bool = True,
    ) -> dict:
        assert allow_audio_regeneration is True
        calls.append((output, visual_plan, saved_audio))
        return {"status": "completed", "block_count": 1, "beat_count": 0}

    monkeypatch.setattr(
        runner,
        "_preflight_stt_alignment_with_retries",
        fake_preflight,
    )

    aligned = asyncio.run(
        runner._preflight_saved_run_audio(tmp_path, plan, run_metadata)
    )

    assert aligned is True
    assert calls == [(tmp_path, plan, audio)]
    assert run_metadata["stt_alignment"] == {
        "status": "completed",
        "block_count": 1,
        "beat_count": 0,
    }


def test_incomplete_saved_audio_does_not_start_stt_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("Incomplete audio must not start paid STT recovery")

    monkeypatch.setattr(
        runner,
        "_preflight_stt_alignment_with_retries",
        forbidden,
    )
    run_metadata: dict[str, object] = {
        "audio_generation": {"status": "completed", "stt_complete": False}
    }

    aligned = asyncio.run(
        runner._preflight_saved_run_audio(tmp_path, {"blocks": []}, run_metadata)
    )

    assert aligned is False
    assert "stt_alignment" not in run_metadata


def test_images_only_refuses_to_replace_images_while_ltx_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script, root, output = _saved_preview(tmp_path)
    pending = output / "video" / "runs" / "old" / "manifest.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(
        json.dumps({"status": "running", "jobs": [{"status": "pending"}]}),
        encoding="utf-8",
    )

    async def forbidden_async(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("Paid/image work must not start while LTX is incomplete")

    def forbidden_sync(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("Existing B2 images must not be deleted")

    monkeypatch.setattr(
        runner, "_preflight_stt_alignment_with_retries", forbidden_async,
    )
    monkeypatch.setattr(runner, "clear_b2_images_for_rerun", forbidden_sync)
    monkeypatch.setattr(runner, "_generate_images", forbidden_async)

    with pytest.raises(SystemExit, match="paid LTX jobs"):
        asyncio.run(runner._run_images_only(script, root))


def test_no_video_images_resume_still_builds_preview_without_ltx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script, root, output = _saved_preview(tmp_path)
    calls: list[str] = []

    async def fake_alignment(*_args: object, **_kwargs: object) -> dict:
        calls.append("alignment")
        return {"status": "completed"}

    async def fake_images(*_args: object, **_kwargs: object) -> dict:
        calls.append("images")
        return {"status": "completed", "items": [], "model_id": "test-images"}

    async def fake_preview(*_args: object, **_kwargs: object) -> dict:
        calls.append("preview")
        return {"status": "completed", "file": "preview/static_preview.mp4"}

    async def forbidden(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("--no-video must never submit an LTX job")

    monkeypatch.setattr(runner, "_preflight_stt_alignment_with_retries", fake_alignment)
    monkeypatch.setattr(runner, "clear_b2_images_for_rerun", lambda *_args: 0)
    monkeypatch.setattr(runner, "_generate_images", fake_images)
    monkeypatch.setattr(runner, "_preview_if_ready", fake_preview)
    monkeypatch.setattr(runner, "_video_if_ready", forbidden)
    awaitable = runner._run_images_only(script, root, no_video=True)
    asyncio.run(awaitable)

    assert calls == ["alignment", "images", "preview"]
    saved = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert saved["run"]["video_preview"]["status"] == "completed"
    assert saved["run"]["video_generation"] == {"status": "skipped"}
    assert saved["run"]["status"] == "completed"
