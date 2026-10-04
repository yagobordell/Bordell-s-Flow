from __future__ import annotations

import hashlib
import json
import re

from .a2v import LTX_A2V_PRODUCTION_FAST_PROFILE
from .model import LTX_PRODUCTION_I2V_PROFILE, LTX_SUPPORTED_I2V_PROFILES


def ltx_video_application_job_id(
    *,
    shot_id: int,
    prompt: str,
    keyframe_sha256: str,
    seed: int,
    width: int,
    height: int,
    fps: int,
    num_frames: int,
    generation_profile: str = LTX_PRODUCTION_I2V_PROFILE,
) -> str:
    """Return the stable application job ID for one LTX-2.5 generation request."""

    if generation_profile not in LTX_SUPPORTED_I2V_PROFILES:
        raise ValueError("Unsupported I2V generation profile")

    plan = {
        "shot_id": shot_id,
        "generation_profile": generation_profile,
        "prompt": prompt,
        "keyframe_sha256": keyframe_sha256,
        "seed": seed,
        "width": width,
        "height": height,
        "fps": fps,
        "num_frames": num_frames,
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"phase8-shot-{shot_id:03d}-{digest[:12]}"


def ltx_a2v_application_job_id(
    *,
    segment_id: str,
    prompt: str,
    image_sha256: str,
    audio_sha256: str,
    seed: int,
    width: int,
    height: int,
    fps: int,
    generation_profile: str = LTX_A2V_PRODUCTION_FAST_PROFILE,
) -> str:
    """Return a stable job ID for one audio-driven LTX segment."""

    normalized_segment_id = re.sub(r"[^A-Za-z0-9._-]+", "-", segment_id).strip("-")
    normalized_segment_id = normalized_segment_id[:40] or "segment"
    plan = {
        "segment_id": segment_id,
        "generation_profile": generation_profile,
        "prompt": prompt,
        "image_sha256": image_sha256,
        "audio_sha256": audio_sha256,
        "seed": seed,
        "width": width,
        "height": height,
        "fps": fps,
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"ltx-a2v-{normalized_segment_id}-{digest[:12]}"
