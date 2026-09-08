from __future__ import annotations

import hashlib
import json

from .ltx_video import LTX_GENERATION_PROFILE


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
    """Return the stable Phase 8 application job ID for one LTX generation request."""

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
