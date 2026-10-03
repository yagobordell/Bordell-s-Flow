"""Summarize repeated A2V benchmarks and compare decoded audio/video hashes.

Exact video hashes are evidence only for unchanged numerical paths. Triton
may round differently; neither matching hashes nor MP4 validity certifies
identity, naturalness or lip-sync, which always need human review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


def _decoded_stream_sha(path: Path, *, audio: bool) -> str:
    command = [
        "ffmpeg", "-v", "error", "-i", str(path), "-map",
        "0:a:0" if audio else "0:v:0",
    ]
    if audio:
        command += ["-ac", "2", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "s16le"]
    else:
        command += ["-pix_fmt", "rgb24", "-f", "rawvideo"]
    command.append("-")
    digest = hashlib.sha256()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise RuntimeError("ffmpeg subprocess pipes unavailable")
    try:
        for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
            digest.update(block)
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        if process.wait() != 0:
            raise RuntimeError(f"ffmpeg decode failed for {path}: {stderr}")
        return digest.hexdigest()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _warm_metric(samples: list[dict[str, Any]], field: str) -> float | None:
    if len(samples) < 2:
        return None
    return statistics.median(float(x[field]) for x in samples[1:])


def analyze_manifest(
    manifest_path: Path, *, hash_media: bool = True, allow_partial: bool = False
) -> dict[str, Any]:
    """Full parity checks by default; partial manifests are diagnostics only."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    runs = manifest["runs"]
    complete = (
        len(runs) == len(manifest["profiles"]) * manifest["repetitions"]
        and not manifest.get("failures")
        and not manifest.get("skipped_profiles")
    )
    if not complete and not allow_partial:
        raise ValueError(
            "The benchmark has incomplete repetitions; use --allow-partial for diagnostics"
        )
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    comparison_rows: list[dict[str, Any]] = []
    control_hashes: dict[str, tuple[str, str]] = {}
    profiles = {
        "reference-bench-cpu": "ltx25-a2v-reference-bench-fp8cpu-eager-a95ab-v1",
        "reference-fp8none": "ltx25-a2v-reference-bench-fp8none-eager-a95ab-v1",
        "reference-cpu-triton": "ltx25-a2v-reference-bench-fp8cpu-triton-a95ab-v1",
        "reference-none-triton": "ltx25-a2v-reference-bench-fp8none-triton-a95ab-v1",
        "static-bench-cpu": "ltx25-a2v-static-bench-fp8cpu-eager-a95ab-v1",
        "static-fp8none": "ltx25-a2v-static-bench-fp8none-eager-a95ab-v1",
    }
    completed_pairs: set[tuple[str, int]] = set()
    for run in runs:
        profile, iteration = run["profile"], int(run["iteration"])
        if profile not in manifest["profiles"] or (profile, iteration) in completed_pairs:
            raise ValueError("Unknown profile or duplicate benchmark iteration")
        completed_pairs.add((profile, iteration))
        meta_path = Path(run["metadata"])
        video_path = Path(run["video"])
        if not meta_path.is_file() or not video_path.is_file():
            raise FileNotFoundError(f"Missing benchmark artifacts: {video_path}")
        metadata = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        if (metadata["seed"], metadata["fps"], metadata["width"], metadata["height"]) != (
            manifest["seed"], 24, 1280, 720
        ):
            raise ValueError("Benchmark changed seed, fps or geometry")
        if bool(metadata["pipeline_reused"]) is not (iteration > 1):
            raise ValueError("Cold/warm labeling is not trustworthy")
        if "prompt_cache_hit" in metadata and (
            bool(metadata["prompt_cache_hit"]) is not (iteration > 1)
        ):
            raise ValueError("Gemma prompt cache labeling is not trustworthy")
        if metadata["generation_profile"] != profiles[profile]:
            raise ValueError("Generation profile mismatch")
        by_profile[profile].append(metadata)
        row: dict[str, Any] = {
            "profile": profile, "iteration": iteration, "video": str(video_path),
        }
        if hash_media:
            row["decoded_audio_sha256"] = _decoded_stream_sha(video_path, audio=True)
            row["decoded_video_sha256"] = _decoded_stream_sha(video_path, audio=False)
            if iteration == 1 and profile in {"reference-bench-cpu", "static-bench-cpu"}:
                control_hashes[profile] = (
                    row["decoded_video_sha256"], row["decoded_audio_sha256"]
                )
        comparison_rows.append(row)

    # Use the matching recipe's control even if FP8 NONE ran before CPU.
    if hash_media:
        for row in comparison_rows:
            control = (
                "static-bench-cpu"
                if row["profile"].startswith("static-")
                else "reference-bench-cpu"
            )
            base = control_hashes.get(control)
            row["comparison_control"] = control
            row["matches_cpu_decoded_video"] = (
                row["decoded_video_sha256"] == base[0] if base else None
            )
            row["matches_cpu_decoded_audio"] = (
                row["decoded_audio_sha256"] == base[1] if base else None
            )

    groups = {}
    for profile, samples in by_profile.items():
        times = [float(x["total_elapsed_seconds"]) for x in samples]
        warm = times[1:]

        groups[profile] = {
            "cold_seconds": times[0],
            "completed_samples": len(samples),
            "warm_samples": len(warm),
            "warm_median_seconds": statistics.median(warm) if warm else None,
            "warm_stdev_seconds": statistics.stdev(warm) if len(warm) > 1 else None,
            "warm_range_seconds": max(warm) - min(warm) if len(warm) > 1 else None,
            "cold_peak_reserved_mb": samples[0].get("cuda_peak_reserved_mb"),
            "warm_peak_reserved_mb": [x.get("cuda_peak_reserved_mb") for x in samples[1:]],
            "stage1_warm_median_seconds": _warm_metric(samples, "stage1_diffusion_seconds"),
            "stage2_warm_median_seconds": _warm_metric(samples, "stage2_diffusion_seconds"),
            "decoder_warm_median_seconds": _warm_metric(samples, "video_decode_seconds"),
            "prompt_warm_median_seconds": _warm_metric(samples, "prompt_encode_seconds"),
        }
    return {
        "source_manifest": str(manifest_path),
        "input_audio_sha256": manifest["audio_sha256"],
        "input_avatar_sha256": manifest["avatar_sha256"],
        "pinned_image": manifest["pinned_image"],
        "lifecycle": manifest["lifecycle"],
        "complete": complete,
        "failures": manifest.get("failures", []),
        "skipped_profiles": manifest.get("skipped_profiles", []),
        "profiles": groups,
        "decoded_media": comparison_rows,
        "benchmark_scope": "Warm prompt cache only applies when prompts are identical",
        "human_lipsync_review": "REQUIRED - not automated",
        "human_avatar_identity_review": "REQUIRED - not automated",
        "promotion_status": "UNVALIDATED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--skip-media-hashes", action="store_true",
        help="Timing-only quick check; never use alone for quality acceptance",
    )
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="Diagnose incomplete/failed runs; never interpret as validated parity",
    )
    args = parser.parse_args()
    result = analyze_manifest(
        args.manifest, hash_media=not args.skip_media_hashes,
        allow_partial=args.allow_partial,
    )
    output = args.manifest.parent / "comparison.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"comparison={output.resolve()}")
    for profile, group in result["profiles"].items():
        median = group["warm_median_seconds"]
        spread = group["warm_stdev_seconds"]
        print(
            f"{profile}: cold={group['cold_seconds']:.2f}s "
            f"warm_median={median if median is not None else 'N/A'} "
            f"warm_stdev={spread if spread is not None else 'N/A'}"
        )
    if not result["complete"]:
        print("PARTIAL BENCHMARK: results are diagnostic only, not a quality gate.")
    print("Manual audio-visual identity and phoneme review required before promotion.")


if __name__ == "__main__":
    main()
