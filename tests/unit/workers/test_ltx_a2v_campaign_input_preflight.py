"""Offline PowerShell regression: identical MP3 input is valid for two distinct A2V avatars."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
CAMPAIGN = ROOT / "scripts" / "smoke" / "run_ltx25_a2v_resolution_batch.ps1"


@pytest.mark.parametrize("same_path", [True, False])
def test_a2v_input_preflight_accepts_identical_mp3_for_two_avatars(
    tmp_path: Path, same_path: bool
) -> None:
    if not all(shutil.which(name) for name in ("pwsh", "ffmpeg", "ffprobe")):
        pytest.skip("PowerShell and FFmpeg are needed for the offline preflight")

    first = tmp_path / "avatar-one.png"
    second = tmp_path / "avatar-two.png"
    Image.new("RGB", (32, 32), (110, 120, 130)).save(first)
    Image.new("RGB", (32, 32), (140, 150, 160)).save(second)
    speech = tmp_path / "speech.mp3"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:a", "libmp3lame", str(speech),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    alternative = speech
    if not same_path:
        alternative = tmp_path / "identical-copy.mp3"
        shutil.copyfile(speech, alternative)

    manifest = json.loads((ROOT / "deploy" / "salad" / "services.json").read_text())
    repo = manifest["services"]["ltx25"]["image"].rsplit(":", maxsplit=1)[0]
    pinned = f"{repo}@sha256:{'0' * 64}"
    common = [
        "pwsh", "-NoProfile", "-File", str(CAMPAIGN),
        "-Avatar720", str(first), "-Audio720", str(speech),
        "-Avatar1080", str(second), "-Audio1080", str(alternative),
        "-ExpectedPinnedImage", pinned, "-Accept1080pOomRisk",
        "-CheckInputsOnly",
    ]
    result = subprocess.run(
        common, cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "identical speech input" in result.stdout
    assert "No Salad or R2 operations were performed" in result.stdout

    # Using identical AVATAR images is still forbidden by the experimental campaign.
    wrong = common.copy()
    wrong[wrong.index("-Avatar1080") + 1] = str(first)
    rejected = subprocess.run(
        wrong, cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert rejected.returncode != 0
    assert "DIFFERENT avatar images" in rejected.stderr
