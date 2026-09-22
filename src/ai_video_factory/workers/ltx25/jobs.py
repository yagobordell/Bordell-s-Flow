from __future__ import annotations

import hashlib
import json

from .a2v import LTX_A2V_GENERATION_PROFILE
from .model import LTX_GENERATION_PROFILE


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
) -> str:
    """Return the stable application job ID for one LTX-2.5 generation request."""

    plan = {
        "shot_id": shot_id,
        "generation_profile": LTX_GENERATION_PROFILE,
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
    avatar_image_sha256: str,
    audio_sha256: str,
    seed: int,
    width: int,
    height: int,
    fps: int,
) -> str:
    """Return a stable application job ID for one audio-driven avatar segment."""

    plan = {
        "segment_id": segment_id,
        "generation_profile": LTX_A2V_GENERATION_PROFILE,
        "prompt": prompt,
        "avatar_image_sha256": avatar_image_sha256,
        "audio_sha256": audio_sha256,
        "seed": seed,
        "width": width,
        "height": height,
        "fps": fps,
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    safe_segment = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in segment_id
    ).strip("-")[:48] or "segment"
    return f"ltx-a2v-{safe_segment}-{digest[:12]}"
