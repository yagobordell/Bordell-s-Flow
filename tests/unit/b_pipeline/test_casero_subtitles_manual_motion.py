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


def test_stt_boundary_slightly_differs_from_snapped_cta_cut() -> None:
    groups = _shift_around_cta(
        [_word("intro.", 0.2, 0.8), _word("next.", 1.01, 1.5)],
        {
            "source_boundary_seconds": 1.01,
            "video_cut_seconds": 1.02,
            "cta_duration_seconds": 5.0,
        },
    )
    assert len(groups[0]) == 1
    assert len(groups[1]) == 1
    assert groups[1][0]["start"] == pytest.approx(6.02)
    assert groups[0][0]["end"] <= 1.02


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


def test_single_long_stt_token_never_creates_two_lines() -> None:
    cues = _captions([[_word("W" * 124, 0, 3.0)]])
    assert len(cues) == 3
    assert all(len(cue["text"]) <= 48 for cue in cues)
    assert all("\n" not in cue["text"] for cue in cues)


def test_short_caption_cannot_leak_into_cta_after_centisecond_rounding() -> None:
    from ai_video_factory.compositor import casero_subtitles as subtitles

    groups = _shift_around_cta(
        [_word("last", 0.992, 0.999), _word("next", 2.001, 2.05)],
        {"source_boundary_seconds": 1.001,
         "video_cut_seconds": 1.0, "cta_duration_seconds": 1.0},
    )
    # The preparation split alone is not the final burn-stage CTA clamp.
    assert len(groups) == 2
    cues = _captions(groups)
    assert cues[0]["start"] < 1.0
    assert subtitles._ass_time(1.99) == "0:00:01.99"


def test_manual_video_shorter_than_second_half_rejected_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.workflows.casero_cta import CaseroCTAError, _adopt_manual_motion

    avatar = _avatar(tmp_path)
    folder = avatar.parent / "meta" / "cta"
    folder.mkdir()
    (folder / "motion_manual.mp4").write_bytes(b"manual fake")
    _stub_ffprobe(monkeypatch, duration=2.0)
    avatar_cta_motion.ensure_avatar_cta_motion(avatar, "manual", interactive=False)
    with pytest.raises(CaseroCTAError, match="necesita al menos"):
        _adopt_manual_motion(folder, half=2.5)
    assert not (folder / "motion_manual_state.json").exists()
    _adopt_manual_motion(folder, half=0.5)
    assert (folder / "motion_manual_state.json").is_file()


def test_automatic_cta_existing_cache_can_still_be_used_without_source_file(
    tmp_path: Path,
) -> None:
    avatar = _avatar(tmp_path)
    folder = avatar.parent / "meta" / "cta"
    folder.mkdir()
    assert avatar_cta_motion.motion_source(folder) == {"mode": "automatic"}


def test_failing_local_ffmpeg_subtitle_burn_can_be_retried_without_paid_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.compositor import casero_subtitles as captions

    output = tmp_path / "work"
    output.mkdir()
    video = output / "original.mp4"
    video.write_bytes(b"original")
    stt = output / "transcript.json"
    audio_hash = "1" * 64
    stt.write_text(json.dumps({
        "source_audio_sha256": audio_hash,
        "words": [{"word": "Hola.", "start": 0.1, "end": 0.7}],
    }), encoding="utf-8")
    audio = {
        "sha256": "2" * 64,
        "block_start_seconds": [0.0],
        "blocks": [{"sha256": audio_hash, "stt": {
            "file": "transcript.json", "sha256": hashlib.sha256(stt.read_bytes()).hexdigest(),
        }}],
    }
    track = {"file": "original.mp4", "sha256": hashlib.sha256(video.read_bytes()).hexdigest()}
    folder = output / "video" / "runs" / "cache"
    folder.mkdir(parents=True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"output")
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stderr="emulated libass failure")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(captions.subprocess, "run", fake_run)
    with pytest.raises(captions.CaseroSubtitleError, match="libass failure"):
        burn_casero_subtitles(output, folder, track, audio)
    assert not (folder / "with_subtitles.part.mp4").exists()
    result = burn_casero_subtitles(output, folder, track, audio)
    assert result["subtitles"]["caption_count"] == 1
    assert len(calls) == 2


def test_per_video_subtitle_selector_preserves_default_and_explicit_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.pipeline.run_b_pipeline import select_casero_subtitles

    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert select_casero_subtitles(None, default=True) is True
    assert select_casero_subtitles(None, default=False) is False
    assert select_casero_subtitles(False, default=True) is False
