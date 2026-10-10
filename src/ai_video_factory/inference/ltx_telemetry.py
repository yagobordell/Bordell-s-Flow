"""Stable, privacy-limited LTX timing records for durable Postgres diagnostics.

No prompts, image contents, audio contents, output paths, or provider credentials
are copied into telemetry. Observations never change inference decisions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .contracts import InferenceJobRequest

_TASK_STAGES = {
    "video.ltx25.generate": "ltx25_i2v",
    "video.ltx25.audio_to_video": "ltx25",
    "video.ltx25.audio_to_video_experimental": "ltx25",
}

_PHASE_KEYS = (
    "pipeline_acquire_seconds",
    "pipeline_build_seconds",
    "pipeline_call_seconds",
    "pipeline_inference_seconds",
    "stage1_total_seconds",
    "stage1_diffusion_seconds",
    "stage2_total_seconds",
    "stage2_diffusion_seconds",
    "video_decode_seconds",
    "audio_decode_seconds",
    "audio_prepare_seconds",
    "video_encode_mux_seconds",
    "encode_mux_excluding_video_decode_seconds",
    "encode_excluding_video_decode_seconds",
    "prompt_encode_seconds",
    "total_generation_seconds",
    "generation_elapsed_seconds",
    "total_elapsed_seconds",
)


def _nonnegative_float(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _first_seconds(source: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        value = _nonnegative_float(source.get(name))
        if value is not None:
            return value
    return None


def build_ltx_job_telemetry(
    request: InferenceJobRequest,
    raw_metrics: Mapping[str, Any] | None,
    *,
    worker_id: str,
    instance_id: str | None,
    attempt_count: int,
    task_seconds: float,
) -> dict[str, Any] | None:
    """Normalize one completed inference attempt; skip incomplete observations."""

    stage = _TASK_STAGES.get(request.task)
    if stage is None or not isinstance(raw_metrics, Mapping):
        return None
    profile = str(request.parameters.get("generation_profile") or "").strip()
    if not profile or attempt_count < 1 or not worker_id.strip():
        return None
    try:
        width = int(raw_metrics.get("internal_width") or request.parameters["width"])
        height = int(raw_metrics.get("internal_height") or request.parameters["height"])
        frames = int(
            raw_metrics.get("frames")
            or raw_metrics.get("num_frames")
            or request.parameters["num_frames"]
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    total = _nonnegative_float(task_seconds)
    if min(width, height, frames) < 1 or total is None:
        return None
    phases = {
        key: value for key in _PHASE_KEYS
        if (value := _nonnegative_float(raw_metrics.get(key))) is not None
    }
    inference = _first_seconds(
        raw_metrics, "pipeline_call_seconds", "pipeline_inference_seconds",
        "inference_seconds",
    )
    load = _first_seconds(
        raw_metrics, "pipeline_acquire_seconds", "pipeline_build_seconds",
        "model_load_seconds",
    )
    reused = raw_metrics.get("pipeline_reused")
    return {
        "job_id": request.job_id,
        "request_sha256": request.fingerprint(),
        "attempt_count": attempt_count,
        "instance_id": instance_id or worker_id,
        "instance_identity_source": "salad_instance" if instance_id else "worker_process",
        "worker_id": worker_id,
        "stage": stage,
        "profile": profile,
        "width": width,
        "height": height,
        "num_frames": frames,
        "task_seconds": total,
        "inference_seconds": inference,
        "pipeline_load_seconds": load,
        "stage1_seconds": _first_seconds(
            raw_metrics, "stage1_total_seconds", "stage1_diffusion_seconds",
        ),
        "stage2_seconds": _first_seconds(
            raw_metrics, "stage2_total_seconds", "stage2_diffusion_seconds",
        ),
        "video_decode_seconds": _first_seconds(raw_metrics, "video_decode_seconds"),
        "encode_seconds": _first_seconds(
            raw_metrics, "encode_excluding_video_decode_seconds",
            "encode_mux_excluding_video_decode_seconds", "video_encode_mux_seconds",
        ),
        "pipeline_reused": reused if isinstance(reused, bool) else None,
        "phase_seconds": phases,
    }
