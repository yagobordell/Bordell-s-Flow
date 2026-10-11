"""The ebook URL is timed by spoken QR, separately from the QR itself."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.compositor.professional_cta_qr import (
    ProfessionalCTAOverlayError,
    apply_professional_ebook_qr,
    build_professional_cta_mux_overlay,
    professional_cta_website_windows,
    professional_cta_windows,
)
from ai_video_factory.inference.storage import sha256_file


def _timeline(*, first_qr=True, second_qr=True):
    return {
        "duration_seconds": 220.0,
        "beats": [
            {
                "beat_id": "1A", "start_seconds": 0.0,
                "end_seconds": 10.0,
                # This is outside a CTA, and must not trigger the URL.
                "qr_spoken_seconds": [3.5],
            },
            {
                "beat_id": "1B", "start_seconds": 10.0,
                "end_seconds": 25.0,
                **({"qr_spoken_seconds": [12.37, 20.5]} if first_qr else {}),
            },
            {"beat_id": "1C", "start_seconds": 25.0, "end_seconds": 35.0},
            {"beat_id": "2A", "start_seconds": 35.0, "end_seconds": 175.0},
            {
                "beat_id": "2B", "start_seconds": 175.0,
                "end_seconds": 185.0,
                **({"qr_spoken_seconds": [179.125]} if second_qr else {}),
            },
            {"beat_id": "2C", "start_seconds": 185.0, "end_seconds": 220.0},
        ],
    }


def _plan():
    return {"ebook_cta_groups": [["1B", "1C"], ["2B"]]}


def test_url_starts_at_first_spoken_qr_per_cta_and_ends_at_cta_end():
    assert professional_cta_website_windows(_timeline(), _plan()) == [
        (12.37, 35.0),
        (179.125, 185.0),
    ]
    assert professional_cta_windows(_timeline(), _plan()) == [
        (10.0, 125.0),
        (175.0, 185.0),
    ]


def test_missing_spoken_qr_never_shows_website_in_that_cta():
    assert professional_cta_website_windows(
        _timeline(first_qr=False), _plan(),
    ) == [(179.125, 185.0)]
    assert professional_cta_website_windows(
        _timeline(first_qr=False, second_qr=False), _plan(),
    ) == []
    assert professional_cta_windows(
        _timeline(first_qr=False, second_qr=False), _plan(),
    ) == [(10.0, 125.0), (175.0, 185.0)]


@pytest.mark.parametrize("value", [-1, 40, float("nan"), float("inf"), True])
def test_invalid_spoken_word_timings_fail_closed(value):
    timeline = _timeline()
    timeline["beats"][1]["qr_spoken_seconds"] = [value]
    with pytest.raises(ProfessionalCTAOverlayError, match="Invalid STT QR onset"):
        professional_cta_website_windows(timeline, _plan())


def _compose(tmp_path, monkeypatch, *, first_qr=True, second_qr=True):
    from ai_video_factory.compositor import b_video, professional_cta_qr

    output = tmp_path / "run"
    run_dir = output / "video" / "runs" / "test"
    run_dir.mkdir(parents=True)
    qr = output / "qr.png"
    Image.new("RGB", (100, 100), "white").save(qr)
    source = output / "original.mp4"
    source.write_bytes(b"fake verified mux")
    commands = []

    def fake_run(args, **kwargs):
        assert kwargs["check"] is False
        commands.append(args)
        output_video = args[-1]
        from pathlib import Path

        Path(output_video).write_bytes(b"composited video with QR and website")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(professional_cta_qr.subprocess, "run", fake_run)
    monkeypatch.setattr(b_video, "_probe_final", lambda *args: None)
    result = apply_professional_ebook_qr(
        output, run_dir,
        {"file": source.name, "sha256": sha256_file(source)},
        _timeline(first_qr=first_qr, second_qr=second_qr),
        {
            **_plan(),
            "avatar": {"qr": {"sha256": sha256_file(qr)}},
        },
    )
    assert len(commands) == 1
    return result, commands[0], run_dir


def test_website_is_small_white_outlined_image_above_bottom_subtitles(
    tmp_path, monkeypatch,
):
    result, cmd, run_dir = _compose(tmp_path, monkeypatch)
    filter_graph = cmd[cmd.index("-filter_complex") + 1]
    assert "between(t\\,10.000000\\,125.000000)" in filter_graph
    assert "between(t\\,175.000000\\,185.000000)" in filter_graph
    assert "gte(t\\,12.370000)*lt(t\\,35.000000)" in filter_graph
    assert "gte(t\\,179.125000)*lt(t\\,185.000000)" in filter_graph
    assert "x=(main_w-overlay_w)/2:y=main_h-overlay_h-145" in filter_graph
    assert cmd.count("-loop") == 2  # One real QR, one rasterized URL
    meta = result["professional_ebook_qr"]
    assert meta["first_cta_tail_seconds"] == 90
    website = meta["website_overlay"]
    assert website["text"] == "zellenne.com"
    assert website["font_size_pixels"] == 46
    assert website["black_outline_pixels"] == 2
    assert website["windows_seconds"] == [[12.37, 35.0], [179.125, 185.0]]
    image = Image.open(run_dir / "ebook_cta_website.png").convert("RGBA")
    pixels = list(image.getdata())
    assert any(r == g == b == 255 and a == 255 for r, g, b, a in pixels)
    assert any(r == g == b == 0 and a == 255 for r, g, b, a in pixels)
    assert any(a == 0 for _r, _g, _b, a in pixels)


def test_no_spoken_qr_uses_only_existing_qr_overlay(tmp_path, monkeypatch):
    result, cmd, run_dir = _compose(
        tmp_path, monkeypatch, first_qr=False, second_qr=False,
    )
    assert cmd.count("-loop") == 1
    assert not (run_dir / "ebook_cta_website.png").exists()
    assert result["professional_ebook_qr"]["website_overlay"]["windows_seconds"] == []


def test_canonical_stt_timeline_uses_real_qr_word_onset(tmp_path):
    import json
    import wave

    from ai_video_factory.compositor.static_preview import build_stt_beat_timeline

    wav = tmp_path / "voice.wav"
    with wave.open(str(wav), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\x00\x00" * 48000 * 2)
    wav_hash = sha256_file(wav)
    transcript = tmp_path / "words.json"
    transcript.write_text(json.dumps({
        "model": "whisper-1", "source_audio_sha256": wav_hash,
        "words": [
            {"word": "Scan", "start": 0.15, "end": 0.25},
            {"word": "the", "start": 0.30, "end": 0.43},
            {"word": "QR", "start": 0.63, "end": 0.75},
            {"word": "now", "start": 0.80, "end": 0.91},
        ],
    }), encoding="utf-8")
    plan = {"blocks": [{"block_id": 1, "beats": [{
        "beat_id": "1A", "text": "Scan the QR now.",
        "visual_type": "media_image", "audio": False,
        "cta_group_id": 1,
    }]}]}
    audio = {
        "file": "voice.wav", "sha256": wav_hash,
        "duration_seconds": 2.0, "stt_complete": True,
        "block_start_seconds": [0.0],
        "block_durations_seconds": [2.0],
        "blocks": [{
            "block_id": 1, "sha256": wav_hash,
            "stt": {
                "file": "words.json", "sha256": sha256_file(transcript),
                "model": "whisper-1",
            },
        }],
    }
    timeline = build_stt_beat_timeline(tmp_path, plan, audio)
    assert timeline["beats"][0]["qr_spoken_seconds"] == [0.63]
    assert professional_cta_website_windows(
        timeline, {"ebook_cta_groups": [["1A"]]},
    ) == [(0.63, 2.0)]


def test_qr_word_timing_does_not_change_paid_ltx_run_fingerprint(tmp_path):
    from ai_video_factory.workflows.b_video import _fingerprint

    avatar = tmp_path / "avatar.png"
    avatar.write_bytes(b"verified-avatar-identity")
    beat = {"beat_id": "1A", "visual_type": "media_image", "audio": False}
    baseline = _fingerprint(
        {"beats": [beat]}, {"sha256": "unchanged-audio"},
        {}, avatar, i2v_profile=None,
    )
    with_url_timing = _fingerprint(
        {"beats": [{**beat, "qr_spoken_seconds": [0.63]}]},
        {"sha256": "unchanged-audio"}, {}, avatar, i2v_profile=None,
    )
    assert with_url_timing == baseline


def test_fused_overlay_uses_existing_cta_timing_and_raster(tmp_path):
    output = tmp_path
    run_dir = tmp_path / "video" / "runs" / "test"
    run_dir.mkdir(parents=True)
    qr = output / "qr.png"
    Image.new("RGB", (100, 100), "white").save(qr)
    inputs, graph, metadata = build_professional_cta_mux_overlay(
        output, run_dir, _timeline(),
        {**_plan(), "avatar": {"qr": {"sha256": sha256_file(qr)}}},
    )
    assert inputs == [
        "-loop", "1", "-i", str(qr),
        "-loop", "1", "-i", str(run_dir / "ebook_cta_website.png"),
    ]
    assert "[0:v]setpts=N/(24*TB),fps=24,format=yuv420p[base]" in graph
    assert "[2:v]scale=280:280:flags=neighbor,format=rgba[qr]" in graph
    assert "[3:v]format=rgba[website]" in graph
    assert r"between(t\,10.000000\,125.000000)" in graph
    assert r"gte(t\,12.370000)*lt(t\,35.000000)" in graph
    assert metadata["website_overlay"]["text"] == "zellenne.com"
    assert metadata["website_overlay"]["windows_seconds"] == [
        [12.37, 35.0], [179.125, 185.0],
    ]


def test_fused_overlay_without_spoken_qr_uses_only_one_image(tmp_path):
    qr = tmp_path / "qr.png"
    Image.new("RGB", (100, 100), "white").save(qr)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs, graph, metadata = build_professional_cta_mux_overlay(
        tmp_path, run_dir, _timeline(first_qr=False, second_qr=False),
        {**_plan(), "avatar": {"qr": {"sha256": sha256_file(qr)}}},
    )
    assert inputs == ["-loop", "1", "-i", str(qr)]
    assert "[3:v]" not in graph
    assert metadata["website_overlay"]["windows_seconds"] == []
    assert not (run_dir / "ebook_cta_website.png").exists()


def test_fused_overlay_rejects_mismatched_qr_before_encoding(tmp_path):
    qr = tmp_path / "qr.png"
    Image.new("RGB", (100, 100), "white").save(qr)
    with pytest.raises(ProfessionalCTAOverlayError, match="does not match"):
        build_professional_cta_mux_overlay(
            tmp_path, tmp_path, _timeline(),
            {**_plan(), "avatar": {"qr": {"sha256": "0" * 64}}},
        )


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg and FFprobe are required for audiovisual parity smoke",
)
def test_fused_professional_cta_encodes_once_with_exact_audio_and_frames(tmp_path):
    qr = tmp_path / "qr.png"
    Image.new("RGB", (100, 100), "white").save(qr)
    base = tmp_path / "base.mp4"
    soundtrack = tmp_path / "master.wav"
    destination = tmp_path / "fused.mp4"

    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:r=24:d=2",
        "-an", "-frames:v", "48", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(base),
    ], check=True, capture_output=True)

    import wave

    with wave.open(str(soundtrack), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\\x00\\x00" * 48000 * 2)

    timeline = {
        "duration_seconds": 2.0,
        "beats": [
            {
                "beat_id": "1A", "start_seconds": 0.0, "end_seconds": 1.0,
                "qr_spoken_seconds": [0.5],
            },
            {"beat_id": "1B", "start_seconds": 1.0, "end_seconds": 2.0},
        ],
    }
    plan = {
        "ebook_cta_groups": [["1A"]],
        "avatar": {"qr": {"sha256": sha256_file(qr)}},
    }
    prepared = build_professional_cta_mux_overlay(
        tmp_path, tmp_path, timeline, plan,
    )
    assert prepared is not None
    inputs, graph, metadata = prepared
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(base), "-i", str(soundtrack), *inputs,
        "-filter_complex", graph, "-map", "[v]", "-map", "1:a:0",
        "-frames:v", "48", "-r", "24", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "19",
        "-video_track_timescale", "12288",
        "-c:a", "aac", "-b:a", "192k", "-t", "2.0",
        "-movflags", "+faststart", str(destination),
    ]
    subprocess.run(command, check=True, capture_output=True)
    report = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,nb_frames,avg_frame_rate:format=duration",
        "-of", "json", str(destination),
    ], check=True, capture_output=True, text=True)
    data = json.loads(report.stdout)
    assert [stream["codec_type"] for stream in data["streams"]] == ["video", "audio"]
    assert int(data["streams"][0]["nb_frames"]) == 48
    assert data["streams"][0]["avg_frame_rate"] == "24/1"
    assert float(data["format"]["duration"]) == pytest.approx(2.0, abs=0.05)
    assert metadata["website_overlay"]["windows_seconds"] == [[0.5, 1.0]]

    def white_text_pixels(position: str) -> int:
        frame = tmp_path / f"frame_{position}.png"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", position, "-i", str(destination), "-frames:v", "1",
            str(frame),
        ], check=True, capture_output=True)
        with Image.open(frame) as image:
            region = image.convert("RGB").crop((700, 855, 1220, 965))
            return sum(
                r > 235 and g > 235 and b > 235
                for r, g, b in region.getdata()
            )

    assert white_text_pixels("0.75") > white_text_pixels("0.25") + 150
    assert white_text_pixels("0.75") > white_text_pixels("1.25") + 150


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg and FFprobe required for actual compositor integration",
)
def test_professional_compositor_fuses_cta_with_final_encode(tmp_path):
    import wave

    from ai_video_factory.compositor.b_video import render_b2_ltx_video

    avatar = tmp_path / "avatar.png"
    media = tmp_path / "image.png"
    qr = tmp_path / "qr.png"
    audio_path = tmp_path / "master.wav"
    Image.new("RGB", (1280, 720), (80, 80, 80)).save(avatar)
    Image.new("RGB", (1280, 720), (100, 100, 100)).save(media)
    Image.new("RGB", (100, 100), (255, 255, 255)).save(qr)
    with wave.open(str(audio_path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\\x00\\x00" * 48000)

    timeline = {
        "duration_seconds": 1.0,
        "beats": [{
            "beat_id": "1A", "block_id": 1,
            "visual_type": "media_image",
            "start_seconds": 0.0, "end_seconds": 1.0,
            "qr_spoken_seconds": [0.25],
        }],
    }
    images = {"items": [{
        "block_id": 1, "beat_id": "1A", "file": media.name,
        "sha256": sha256_file(media),
    }]}
    manifest = {
        "avatar_file": avatar.name, "avatar_sha256": sha256_file(avatar),
        "jobs": [],
    }
    audio = {"file": audio_path.name, "sha256": sha256_file(audio_path)}
    plan = {
        "ebook_cta_groups": [["1A"]],
        "avatar": {"qr": {"sha256": sha256_file(qr)}},
    }
    run_dir = tmp_path / "video" / "runs" / "fused"
    run_dir.mkdir(parents=True)
    result = render_b2_ltx_video(
        tmp_path, run_dir, timeline, [], manifest, images, audio,
        cta_plan=plan,
    )
    assert result["file"] == "video/runs/fused/final.mp4"
    assert not (run_dir / "with_ebook_qr.mp4").exists()
    assert result["sha256"] == sha256_file(tmp_path / result["file"])
    assert result["professional_ebook_qr"]["website_overlay"]["windows_seconds"] == [
        [0.25, 1.0],
    ]
    assert result["fps"] == 24
    assert result["beat_count"] == 1
    assert result["rendered_duration_seconds"] == pytest.approx(1.0, abs=0.15)
    assert result["composition_timings_seconds"]["final_encoding"] >= 0
