"""Offline comparison gate for LTX-2.5 A2V experiments.

This module does not run inference or infer visual quality. Visual approval
is a human attestation, never a conclusion derived from media metadata.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUEST_FIELDS = ("prompt", "seed", "width", "height", "fps", "generation_profile")
METADATA_RECIPE_FIELDS = (
    "generation_profile",
    "generation_recipe",
    "ltx_model_revision",
    "transformer_variant",
    "quantization",
    "offload_mode",
    "stage_1_sampler",
    "stage_2_sampler",
    "stage_1_steps",
    "stage_2_steps",
    "stage_1_image_strength",
    "stage_2_image_strength",
    "audio_frozen_stage_1",
    "audio_frozen_stage_2",
    "video_cfg_scale",
    "video_stg_scale",
    "video_modality_scale",
    "video_rescale_scale",
    "video_stg_blocks",
    "fps",
    "num_frames",
    "width",
    "height",
    "seed",
    "input_audio_sample_rate",
    "decoded_speech_samples",
    "conditioning_audio_samples",
    "audio_padding_samples",
)
TIMING_FIELDS = (
    "model_load_seconds",
    "inference_seconds",
    "video_encode_mux_seconds",
    "generation_elapsed_seconds",
    "total_elapsed_seconds",
)


def _read_object(path: Path) -> dict[str, Any]:
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return content


def _input_hashes(
    request: dict[str, Any], *, label: str, problems: list[str]
) -> dict[str, str]:
    inputs = request.get("inputs")
    if not isinstance(inputs, list):
        problems.append(f"{label}: request.inputs must be an array")
        return {}
    hashes: dict[str, str] = {}
    for item in inputs:
        if not isinstance(item, dict):
            problems.append(f"{label}: request.inputs contains a non-object")
            continue
        name, digest = item.get("name"), item.get("sha256")
        if name not in ("image", "audio"):
            continue
        if name in hashes:
            problems.append(f"{label}: duplicate {name} input")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            problems.append(f"{label}: invalid SHA-256 for {name}")
            continue
        hashes[name] = digest
    for name in ("image", "audio"):
        if name not in hashes:
            problems.append(f"{label}: missing valid {name} input hash")
    return hashes


def _compare_fields(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    fields: tuple[str, ...],
    *,
    scope: str,
    problems: list[str],
) -> None:
    for field in fields:
        if field not in baseline or field not in candidate:
            problems.append(f"{scope}.{field}: missing from one or both runs")
        elif baseline[field] != candidate[field]:
            problems.append(f"{scope}.{field}: values differ")


def _validate_provenance(
    *,
    label: str,
    commit: str | None,
    docker_digest: str | None,
    problems: list[str],
) -> None:
    if commit is None or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        problems.append(f"{label}: complete Git commit SHA is required")
    if docker_digest is None or re.fullmatch(r"sha256:[0-9a-f]{64}", docker_digest) is None:
        problems.append(f"{label}: immutable Docker sha256 digest is required")


def compare_runs(
    *,
    baseline_request: dict[str, Any],
    baseline_metadata: dict[str, Any],
    candidate_request: dict[str, Any],
    candidate_metadata: dict[str, Any],
    baseline_commit: str | None = None,
    candidate_commit: str | None = None,
    baseline_docker_digest: str | None = None,
    candidate_docker_digest: str | None = None,
    baseline_visual_approved: bool = False,
    candidate_visual_approved: bool = False,
    expected_image_sha256: str | None = None,
    expected_audio_sha256: str | None = None,
) -> dict[str, Any]:
    """Check recorded comparability; never approve lip-sync automatically."""
    problems: list[str] = []
    baseline_hashes = _input_hashes(baseline_request, label="baseline", problems=problems)
    candidate_hashes = _input_hashes(
        candidate_request, label="candidate", problems=problems
    )
    if baseline_hashes != candidate_hashes:
        problems.append("input SHA-256 hashes differ between runs")

    for kind, expected in (
        ("image", expected_image_sha256),
        ("audio", expected_audio_sha256),
    ):
        if expected is not None and baseline_hashes.get(kind) != expected:
            problems.append(f"baseline {kind} SHA-256 differs from expected")

    baseline_job = baseline_request.get("job_id")
    candidate_job = candidate_request.get("job_id")
    if not baseline_job or not candidate_job:
        problems.append("both requests must record their job_id")
    elif baseline_job == candidate_job:
        problems.append("job_id must be fresh to prevent replay of an earlier artifact")

    baseline_params = baseline_request.get("parameters")
    candidate_params = candidate_request.get("parameters")
    if not isinstance(baseline_params, dict) or not isinstance(candidate_params, dict):
        problems.append("both requests must contain parameters objects")
    else:
        _compare_fields(
            baseline_params, candidate_params, REQUEST_FIELDS,
            scope="request.parameters", problems=problems,
        )
    _compare_fields(
        baseline_metadata, candidate_metadata, METADATA_RECIPE_FIELDS,
        scope="metadata", problems=problems,
    )

    baseline_reused = baseline_metadata.get("pipeline_reused")
    candidate_reused = candidate_metadata.get("pipeline_reused")
    if not isinstance(baseline_reused, bool) or not isinstance(candidate_reused, bool):
        problems.append("both metadata files must record boolean pipeline_reused")
    elif baseline_reused != candidate_reused:
        problems.append("cold and reused-pipeline generation must not be compared as peers")

    for label, metadata in (
        ("baseline", baseline_metadata),
        ("candidate", candidate_metadata),
    ):
        for field in TIMING_FIELDS:
            value = metadata.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                problems.append(f"{label}: missing or invalid {field}")

    _validate_provenance(
        label="baseline", commit=baseline_commit,
        docker_digest=baseline_docker_digest, problems=problems,
    )
    _validate_provenance(
        label="candidate", commit=candidate_commit,
        docker_digest=candidate_docker_digest, problems=problems,
    )
    if not baseline_visual_approved:
        problems.append("baseline: human visual lip-sync approval is not recorded")
    if not candidate_visual_approved:
        problems.append("candidate: human visual lip-sync approval is not recorded")

    comparable = not problems
    return {
        "can_compare_generation_times": comparable,
        "quality_inferred_from_technical_checks": False,
        "timing_scope": (
            "worker timings only; inference_seconds includes audio preparation inside "
            "the pipeline and video decode. Excludes Salad allocation, Docker/model "
            "download, Postgres wait and artifact transfer."
        ),
        "pipeline_temperature": (
            "warm" if baseline_reused is True and candidate_reused is True
            else "cold" if baseline_reused is False and candidate_reused is False
            else "not_comparable"
        ),
        "blocking_reasons": problems,
        "provenance": {
            "baseline": {
                "job_id": baseline_job,
                "commit": baseline_commit,
                "docker_digest": baseline_docker_digest,
                "input_hashes": baseline_hashes,
            },
            "candidate": {
                "job_id": candidate_job,
                "commit": candidate_commit,
                "docker_digest": candidate_docker_digest,
                "input_hashes": candidate_hashes,
            },
        },
        "worker_timings": {
            "baseline": {field: baseline_metadata.get(field) for field in TIMING_FIELDS},
            "candidate": {field: candidate_metadata.get(field) for field in TIMING_FIELDS},
        },
        "candidate_minus_baseline_inference_seconds": (
            round(
                float(candidate_metadata["inference_seconds"])
                - float(baseline_metadata["inference_seconds"]),
                6,
            )
            if comparable
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline LTX A2V comparability gate; no GPU or external services."
    )
    for prefix in ("baseline", "candidate"):
        parser.add_argument(f"--{prefix}-request", type=Path, required=True)
        parser.add_argument(f"--{prefix}-metadata", type=Path, required=True)
        parser.add_argument(f"--{prefix}-commit", required=True)
        parser.add_argument(f"--{prefix}-docker-digest", required=True)
        parser.add_argument(f"--{prefix}-visual-approved", action="store_true")
    parser.add_argument("--expected-image-sha256")
    parser.add_argument("--expected-audio-sha256")
    parser.add_argument(
        "--report", type=Path,
        help="Write a new JSON report; refuses to overwrite an existing file.",
    )
    args = parser.parse_args()
    report = compare_runs(
        baseline_request=_read_object(args.baseline_request),
        baseline_metadata=_read_object(args.baseline_metadata),
        candidate_request=_read_object(args.candidate_request),
        candidate_metadata=_read_object(args.candidate_metadata),
        baseline_commit=args.baseline_commit,
        candidate_commit=args.candidate_commit,
        baseline_docker_digest=args.baseline_docker_digest,
        candidate_docker_digest=args.candidate_docker_digest,
        baseline_visual_approved=args.baseline_visual_approved,
        candidate_visual_approved=args.candidate_visual_approved,
        expected_image_sha256=args.expected_image_sha256,
        expected_audio_sha256=args.expected_audio_sha256,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0 if report["can_compare_generation_times"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
