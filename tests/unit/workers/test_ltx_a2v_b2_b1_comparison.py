"""No-GPU A2V B1 comparator checks: FFmpeg parser and relative speech timing."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def comparison(monkeypatch: pytest.MonkeyPatch):
    # This CLI deliberately uses a sibling script import when invoked directly
    # from PowerShell. Ensure the same module is available in pytest's context.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts/smoke"))
    sys.modules.pop("scripts.smoke.compare_ltx25_a2v_b2_vs_b1", None)
    return importlib.import_module("scripts.smoke.compare_ltx25_a2v_b2_vs_b1")


def test_real_ffmpeg_stats_patterns_include_full_ssim_values(
    comparison, tmp_path: Path,
) -> None:
    ref, out = tmp_path / "b1.mp4", tmp_path / "b2.mp4"
    ref.write_bytes(b"stub")
    out.write_bytes(b"stub")

    def stub_ffmpeg(args, *, cwd, **_kwargs):
        kind = args[args.index("-lavfi") + 1].split("=")[0]
        if kind == "ssim":
            (Path(cwd) / "scores.txt").write_text(
                "n:1 Y:0.99 U:0.99 V:0.99 All:0.993715 (21.4)\n"
                "n:2 Y:0.98 U:0.99 V:0.99 All:0.990123 (20.1)\n"
            )
        else:
            (Path(cwd) / "scores.txt").write_text(
                "n:1 mse_avg:0.15 psnr_avg:48.51\n"
                "n:2 mse_avg:0.16 psnr_avg:47.90\n"
            )
        return SimpleNamespace(returncode=0, stderr="")

    with (
        patch.object(comparison.shutil, "which", return_value="ffmpeg"),
        patch.object(comparison.subprocess, "run", side_effect=stub_ffmpeg),
    ):
        psnr = comparison._metric(ref, out, "psnr", tmp_path / "scores.txt")
        ssim = comparison._metric(ref, out, "ssim", tmp_path / "scores.txt")
    assert psnr["frames_compared"] == 2
    assert psnr["mean"] == pytest.approx(48.205)
    assert ssim["frames_compared"] == 2
    assert ssim["mean"] == pytest.approx(0.991919)


def test_envelope_comparator_detects_relative_audio_shift(
    comparison, tmp_path: Path,
) -> None:
    audio, baseline, candidate = (tmp_path / f"{x}.wav" for x in ("audio", "b1", "b2"))
    for path in (audio, baseline, candidate):
        path.write_bytes(b"audio")
    spoken = [
        float(500 if x % 23 == 3 else 2100 if x % 23 == 8 else 1000
              if x % 23 == 13 else 100)
        for x in range(150)
    ]
    shifted = [100.0, 100.0, *spoken[:-2]]
    with (
        patch.object(
            comparison, "_voice_rms_profile",
            side_effect=[(spoken, 0.02), (spoken, 0.02), (shifted, 0.02)],
        ),
        patch.object(comparison, "_metric", return_value={"frames_compared": 121}),
    ):
        result = comparison.compare_one(
            label="a", audio=audio, baseline=baseline, candidate=candidate,
            destination=tmp_path,
        )
    assert result["audio"]["b1_vs_source_lag_seconds"] == 0
    assert result["audio"]["b2_vs_source_lag_seconds"] == pytest.approx(0.04)
    assert result["audio"]["lag_comparison"] == "NO_REGRESSION_DETECTED"
    assert result["lipsync_status"] == "HUMAN_SIDE_BY_SIDE_REVIEW_REQUIRED"
