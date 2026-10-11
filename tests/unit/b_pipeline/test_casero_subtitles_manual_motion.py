"""Offline casero subtitle and manual CTA motion selection contracts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.compositor.casero_subtitles import (
    _captions,
    _shift_around_cta,
    _subtitle_files,
    burn_casero_subtitles,
)
from ai_video_factory.providers import avatar_cta_motion


def _word(text: str, start: float, end: float) -> dict:
    return {"text": text, "start": start, "end": end}


def test_captions_are_single_line_without_black_background() -> None:
    words = [
        _word("This", 0, 0.25), _word("is", 0.25, 0.45),
        _word("a", 0.45, 0.57), _word("subtitle.", 0.57, 1.5),
    ]
    cues = _captions([words])
    assert [cue["text"] for cue in cues] == ["This is a subtitle."]
    srt, ass = _subtitle_files(cues)
    assert "This is a subtitle." in srt
    assert "Arial,30," in ass
    assert "&H00FFFFFF" in ass
    assert "0,2,2,65,65,60,1" in ass  # zero outline, shadow 2
    assert "WrapStyle: 2" in ass
    assert r"{\q2}" in ass
    assert "\\N" not in ass
    assert "BorderStyle: 3" not in ass


def test_long_text_creates_multiple_single_line_cues() -> None:
    words = [_word("palabra", n * 0.22, n * 0.22 + 0.15) for n in range(30)]
    cues = _captions([words])
    assert len(cues) > 1
    assert all(len(cue["text"]) <= 48 for cue in cues)
    assert all("\n" not in cue["text"] for cue in cues)


def test_cta_interval_is_always_empty_and_later_cues_are_shifted() -> None:
    cues = _captions(_shift_around_cta(
        [_word("before.", 0.3, 0.7), _word("after.", 4.0, 4.5)],
        {"video_cut_seconds": 2.0, "cta_duration_seconds": 8.0},
    ))
    assert [(round(cue["start"], 1), cue["text"]) for cue in cues] == [
        (0.3, "before."), (12.0, "after."),
    ]
    assert not any(cue["start"] < 10 and cue["end"] > 2 for cue in cues)


def test_subtitle_text_cannot_inject_ass_overrides() -> None:
    _, ass = _subtitle_files([{
        "start": 1.0, "end": 2.0,
        "text": "{\\pos(1,1)} accidental\nnew line",
    }])
    assert "pos(1,1)" in ass
    assert r"{\pos" not in ass
    assert r"\N" not in ass


def _avatar(tmp_path: Path) -> Path:
    avatar = tmp_path / "data" / "enzo" / "enzo.png"
    avatar.parent.mkdir(parents=True)
    avatar.write_bytes(b"not decoded here")
    (avatar.parent / "meta").mkdir()
    return avatar


def _stub_ffprobe(monkeypatch: pytest.MonkeyPatch, *, duration: float = 18.0) -> None:
    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout=json.dumps({
            "streams": [{
                "codec_type": "video", "codec_name": "h264",
                "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
            }],
            "format": {"duration": str(duration)},
        }))
    monkeypatch.setattr(avatar_cta_motion.subprocess, "run", fake_run)


def test_automatic_motion_is_default_and_persistent(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path)
    source = avatar_cta_motion.ensure_avatar_cta_motion(avatar, interactive=False)
    assert source["mode"] == "automatic"
    assert avatar_cta_motion.motion_source(avatar.parent / "meta" / "cta") == source
    with pytest.raises(SystemExit, match="ya está configurado"):
        avatar_cta_motion.ensure_avatar_cta_motion(avatar, "manual")


def test_manual_motion_is_pinned_and_never_replaced_implicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = _avatar(tmp_path)
    source_file = avatar.parent / "meta" / "cta" / "motion_manual.mp4"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"fake mp4 content")
    _stub_ffprobe(monkeypatch)
    source = avatar_cta_motion.ensure_avatar_cta_motion(
        avatar, "manual", interactive=False,
    )
    assert source["mode"] == "manual"
    assert source["sha256"] == hashlib.sha256(b"fake mp4 content").hexdigest()
    assert avatar_cta_motion.ensure_avatar_cta_motion(avatar) == source
    source_file.write_bytes(b"changed content")
    with pytest.raises(SystemExit, match="SHA-256"):
        avatar_cta_motion.motion_source(source_file.parent)


def test_existing_cta_keeps_automatic_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    avatar = _avatar(tmp_path)
    cache = avatar.parent / "meta" / "cta"
    cache.mkdir()
    (cache / "completed.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Legacy CTA prompted"))
    stored = avatar_cta_motion.ensure_avatar_cta_motion(avatar, interactive=True)
    assert stored["mode"] == "automatic"


def test_subtitle_render_uses_only_narration_stt_and_never_cta_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.compositor import casero_subtitles as subtitles

    output = tmp_path / "out"
    run = output / "video" / "runs" / "test"
    run.mkdir(parents=True)
    original = output / "base.mp4"
    original.write_bytes(b"verified unsubtitled video")
    audio_sha = "a" * 64
    transcript = output / "stt.json"
    transcript.write_text(json.dumps({
        "source_audio_sha256": audio_sha,
        "words": [
            {"word": "before.", "start": 0.1, "end": 0.6},
            {"word": "after.", "start": 2.0, "end": 2.6},
        ],
    }), encoding="utf-8")
    audio = {
        "sha256": "b" * 64,
        "block_start_seconds": [0.0],
        "blocks": [{"sha256": audio_sha, "stt": {
            "file": "stt.json", "sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
        }}],
    }
    video = {
        "file": "base.mp4", "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "cta_insertion": {"video_cut_seconds": 1.0, "cta_duration_seconds": 6.0},
    }
    calls: list[list[str]] = []

    def fake_ffmpeg(command: list[str], **kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"burned after CTA insertion")
        assert kwargs["cwd"] == run
        assert "ass=subtitles.ass" in command
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subtitles.subprocess, "run", fake_ffmpeg)
    result = burn_casero_subtitles(output, run, video, audio)
    assert result["subtitles"]["enabled"] is True
    assert result["subtitles"]["caption_count"] == 2
    content = (run / "subtitles.srt").read_text(encoding="utf-8")
    assert "00:00:00,100 --> 00:00:00,600" in content
    assert "00:00:08,000 --> 00:00:08,600" in content
    assert "00:00:01,000 -->" not in content
    assert len(calls) == 1

    # Cached local render must not encode again.
    monkeypatch.setattr(
        subtitles.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("Re-encoded valid subtitle cache"),
    )
    assert burn_casero_subtitles(output, run, video, audio) == result
