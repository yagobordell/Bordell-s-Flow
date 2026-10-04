"""Compare isolated I2V Triton outputs with the EXACT prior eager 1080p pair.

This is an evidence report, not automatic quality approval. Reject mismatched
keyframes, prompt texts, seeds, video recipe or geometry before computing
per-frame decoded PSNR and SSIM. Different inputs make kernel comparisons invalid.
Both videos still require side-by-side human review for perceptual defects.
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


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing controlled-reference file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_matched_inputs(baseline: Path, candidate: Path) -> dict[str, Any]:
    old = _read(baseline / "reference-provenance.json")["inputs"]
    new = _read(candidate / "reference-provenance.json")["inputs"]
    for prop in ("inputs", "video_width", "video_height", "full_hd_stage2_mode"):
        if old[prop] != new[prop]:
            raise ValueError(f"Invalid backend comparison: {prop} changed")
    for directory, backend in ((baseline, "eager"), (candidate, "triton_experimental")):
        request = _read(directory / "job-request.json")["parameters"]
        if request.get("vae_backend", "eager") != backend:
            raise ValueError(f"Invalid reference: expected {backend} backend in {directory}")
        if request.get("allow_sequential_fallback") or request.get("reuse_transformer_across_stages"):
            raise ValueError("Backend comparison requires real independent-stage true B2")
        for field in ("a", "b"):
            expected = next(x for x in old["inputs"] if x["name"] == f"keyframe_{field}")
            sample = request[field]
            if sample["prompt"] != expected["prompt"] or sample["seed"] != expected["seed"]:
                raise ValueError("Prompt or seed differs between provenance and request")
            if (
                sample["width"], sample["height"], sample["fps"], sample["num_frames"]
            ) != (1920, 1080, 24, 121):
                raise ValueError("Backend comparison requires identical Full HD video recipe")
    return {"matching_inputs": True, "baseline_backend": "eager", "candidate_backend": "triton_experimental"}


def _ffmpeg_scores(
    ffmpeg: str, baseline: Path, candidate: Path, kind: str, stats: Path,
) -> dict[str, float | int | None]:
    args = [
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
        "-i", str(baseline), "-i", str(candidate),
        "-lavfi", f"{kind}=stats_file={stats.as_posix()}",
        "-an", "-f", "null", "-",
    ]
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=600)
    lines = stats.read_text(encoding="utf-8").splitlines()
    pattern = r"psnr_avg:([^\s]+)" if kind == "psnr" else r"All:([^\s]+)"
    values = [
        float(match.group(1))
        for line in lines
        if (match := re.search(pattern, line)) is not None
    ]
    if len(values) != 121:
        raise RuntimeError(f"{kind} produced {len(values)} frames, expected 121")
    finite = [v for v in values if math.isfinite(v)]
    return {
        "frames": len(values),
        "average": round(fmean(finite), 6) if len(finite) == len(values) else None,
        "minimum": round(min(finite), 6) if finite else None,
        "maximum": round(max(finite), 6) if finite else None,
        "all_identical": bool(kind == "psnr" and not finite),
    }


def compare(baseline: Path, candidate: Path) -> dict[str, Any]:
    match = verify_matched_inputs(baseline, candidate)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for decoded-frame backend comparison")
    scores = {}
    for label in ("a", "b"):
        source = baseline / f"batch2-{label}.mp4"
        result = candidate / f"batch2-{label}.mp4"
        for path in (source, result):
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Missing or empty video: {path}")
        scores[label] = {}
        for kind in ("psnr", "ssim"):
            stats = candidate / f"backend-compare-{label}-{kind}.txt"
            scores[label][kind] = _ffmpeg_scores(ffmpeg, source, result, kind, stats)
    report = {
        **match,
        "metrics": scores,
        "quality_status": "HUMAN_SIDE_BY_SIDE_REVIEW_REQUIRED",
        "explanation": (
            "Decoded-frame metrics show numerical differences only. "
            "Visual acceptance and RTX 5090 stability/performance are separate gates."
        ),
    }
    output = candidate / "backend-quality-comparison.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.baseline_dir, args.candidate_dir), indent=2))


if __name__ == "__main__":
    main()
