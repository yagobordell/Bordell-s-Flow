"""Measure each 1080p A2V B2 result against its corresponding B1 reference.

Neither high PSNR nor aligned audio envelopes can prove perceptual lipsync.
Both full five-second paired clips still require human side-by-side review.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from statistics import fmean
from typing import Any

from ai_video_factory.inference.storage import sha256_file
from submit_ltx25_a2v_smoke import _voice_rms_profile


def _metric(
    reference: Path, candidate: Path, kind: str, log: Path,
) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for actual B1 comparison")
    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
            "-i", str(reference.resolve()),
            "-i", str(candidate.resolve()),
            "-lavfi", f"{kind}=stats_file={log.name}",
            "-an", "-f", "null", "-",
        ],
        cwd=log.parent.resolve(),
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode:
        raise RuntimeError(f"{kind} comparison failed: {result.stderr[-1000:]}")
    pattern = r"psnr_avg:([^\s]+)" if kind == "psnr" else r"All:([^\\s]+)"
    values = [
        float(match.group(1))
        for line in log.read_text().splitlines()
        if (match := re.search(pattern, line))
    ]
    if not values:
        raise RuntimeError("FFmpeg returned no comparison frames")
    finite = [v for v in values if math.isfinite(v)]
    return {
        "frames_compared": len(values),
        "mean": fmean(finite) if len(finite) == len(values) else None,
        "min": min(finite) if finite else None,
        "all_bit_identical": kind == "psnr" and not finite,
    }


def _envelope_lag(source: list[float], target: list[float]) -> int:
    # A non-silent 20ms speech envelope supplies an independent A/V time
    # alignment check; account for AAC delay without modifying any audio.
    length = min(len(source), len(target))
    source = source[:length]
    target = target[:length]
    if length < 20 or max(source) < 300 or max(target) < 300:
        raise RuntimeError("Speech envelope too short or silent for sync comparison")

    def correlation(shift: int) -> float:
        pairs = [
            (source[i], target[i + shift])
            for i in range(max(0, -shift), min(length, length - shift))
        ]
        if len(pairs) < 10:
            return -2.0
        mx = fmean(x for x, _ in pairs)
        my = fmean(y for _, y in pairs)
        cross = sum((x - mx) * (y - my) for x, y in pairs)
        vx = sum((x - mx) ** 2 for x, _ in pairs)
        vy = sum((y - my) ** 2 for _, y in pairs)
        return cross / math.sqrt(vx * vy) if vx > 0 and vy > 0 else -2.0

    return max(range(-10, 11), key=correlation)


def compare_one(
    *, label: str, audio: Path, baseline: Path, candidate: Path, destination: Path,
) -> dict[str, Any]:
    for path in (audio, baseline, candidate):
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Missing comparison artifact: " + str(path))
    input_envelope, input_window = _voice_rms_profile(audio)
    baseline_envelope, baseline_window = _voice_rms_profile(baseline)
    candidate_envelope, candidate_window = _voice_rms_profile(candidate)
    if max(abs(input_window - baseline_window), abs(input_window - candidate_window)) > 1e-9:
        raise RuntimeError("Audio reference timing grids differ")
    baseline_lag = _envelope_lag(input_envelope, baseline_envelope)
    candidate_lag = _envelope_lag(input_envelope, candidate_envelope)
    return {
        "input_sha256": sha256_file(audio),
        "baseline_video": str(baseline),
        "candidate_video": str(candidate),
        "video": {
            kind: _metric(
                baseline, candidate, kind, destination / f"b1-{label}-{kind}.txt",
            )
            for kind in ("psnr", "ssim")
        },
        "audio": {
            "b1_vs_source_lag_seconds": baseline_lag * input_window,
            "b2_vs_source_lag_seconds": candidate_lag * input_window,
            "lag_delta_seconds": (candidate_lag - baseline_lag) * input_window,
            "lag_comparison": (
                "NO_REGRESSION_DETECTED"
                if abs(candidate_lag - baseline_lag) <= 2
                else "REVIEW_AUDIO_TIMING"
            ),
        },
        "lipsync_status": "HUMAN_SIDE_BY_SIDE_REVIEW_REQUIRED",
    }


def compare(
    *, audio_a: Path, audio_b: Path, baseline_a: Path, baseline_b: Path,
    candidate_a: Path, candidate_b: Path, destination: Path,
) -> dict[str, Any]:
    report = {
        "a": compare_one(
            label="a", audio=audio_a, baseline=baseline_a,
            candidate=candidate_a, destination=destination,
        ),
        "b": compare_one(
            label="b", audio=audio_b, baseline=baseline_b,
            candidate=candidate_b, destination=destination,
        ),
        "comparison_input_provenance": "OPERATOR_MUST_CONFIRM_MATCHED_B1_PROMPTS_SEEDS_IMAGES",
        "quality_status": "HUMAN_B1_LIPSYNC_AND_MOTION_REVIEW_REQUIRED",
    }
    (destination / "b1-comparison.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("audio-a", "audio-b", "baseline-a", "baseline-b",
                 "candidate-a", "candidate-b", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = vars(parser.parse_args())
    print(json.dumps(compare(**{
        name.replace("-", "_"): value for name, value in args.items()
    }), indent=2))
