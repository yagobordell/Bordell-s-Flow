from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from scripts.pipeline import run_vertical_a2v as vertical


def test_vertical_dimensions_are_native_grid_9_by_16() -> None:
    assert vertical.HEIGHT * 9 == vertical.WIDTH * 16
    assert vertical.WIDTH % 64 == vertical.HEIGHT % 64 == 0


@pytest.mark.parametrize("seconds", [0.2, 5.7, 11.25, 12, 15.386, 23, 61, 135])
def test_planner_covers_every_second_without_oversized_jobs(seconds: float) -> None:
    result, forced = vertical.plan_segments(seconds, [])
    assert result[0][0] == 0.0
    assert result[-1][1] == seconds
    assert forced == max(0, len(result) - 1)
    for (start, end), next_part in zip(result, result[1:]):
        assert start < end
        assert end - start <= vertical.MAX_SEGMENT + 0.0001
        assert next_part[0] == end


def test_planner_prefers_natural_pause_and_does_not_make_tiny_tail() -> None:
    parts, forced = vertical.plan_segments(15.386, [1.0, 7.8, 14.8])
    assert forced == 0
    assert parts == [(0, 7.8), (7.8, 15.386)]
    nearly_limit, forced = vertical.plan_segments(11.30, [11.0])
    assert forced == 1
    assert min(end - start for start, end in nearly_limit) > 5


def test_nonpositive_length_is_rejected() -> None:
    with pytest.raises(ValueError):
        vertical.plan_segments(0, [])


def test_portrait_input_keeps_subject_uncropped(tmp_path: Path) -> None:
    # An original wide portrait has a bright center; fitted content remains visible.
    source = tmp_path / "image.jpeg"
    Image.new("RGB", (1280, 720), (230, 20, 20)).save(source)
    result = tmp_path / "portrait.png"
    vertical.prepare_portrait(source, result)
    with Image.open(result) as opened:
        assert opened.size == (576, 1024)
        assert opened.getpixel((288, 512))[0] > 210


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="requires ffmpeg"
)
def test_real_offline_plan_is_resumable_and_mp3_safe(tmp_path: Path) -> None:
    image = tmp_path / "avatar.jpg"
    Image.new("RGB", (600, 800), (50, 90, 130)).save(image)
    mp3 = tmp_path / "voice.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=500:duration=15.386", "-c:a", "libmp3lame", str(mp3),
    ], check=True)
    script = tmp_path / "guion.txt"
    script.write_text("Guion de la voz", encoding="utf-8")
    folder = tmp_path / "output"
    flags = [
        "--image", str(image), "--audio", str(mp3), "--script", str(script),
        "--output-dir", str(folder), "--plan-only",
    ]
    assert vertical.main(flags) == 0
    first = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert len(first["segments"]) == 2
    assert sum(x["duration"] for x in first["segments"]) == pytest.approx(15.386, abs=0.05)
    assert all(x["duration"] <= 11.26 for x in first["segments"])
    assert (folder / "work" / "reference.png").is_file()
    assert vertical.main(flags) == 0
    assert json.loads((folder / "manifest.json").read_text(encoding="utf-8")) == first
    # Correcting the optional transcript must not require a fresh GPU inference.
    script.write_text("Nuevo guion", encoding="utf-8")
    assert vertical.main(flags) == 0
    corrected = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert corrected["identity"] == first["identity"]
    assert corrected["script_sha256"] != first["script_sha256"]
    # A changed source image cannot reuse existing generated clips.
    Image.new("RGB", (600, 800), (200, 90, 130)).save(image)
    with pytest.raises(SystemExit, match="different inputs/cuts"):
        vertical.main(flags)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="requires ffmpeg"
)
def test_real_ffmpeg_portrait_composition_uses_master_audio(tmp_path: Path) -> None:
    clips = []
    for index in range(2):
        clip = tmp_path / f"clip-{index}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=576x1024:r=24:d=0.6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.6",
            "-c:v", "libx264", "-c:a", "aac", "-t", "0.6", str(clip),
        ], check=True)
        clips.append(clip)
    master = tmp_path / "master.wav"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=720:duration=1.1",
        "-c:a", "pcm_s16le", str(master),
    ], check=True)
    output = tmp_path / "vertical.mp4"
    vertical.compose(clips, [0.55, 0.55], master, output, full_hd=False)
    vertical._check_video(output)
    assert vertical._duration(output) == pytest.approx(1.1, abs=0.1)
