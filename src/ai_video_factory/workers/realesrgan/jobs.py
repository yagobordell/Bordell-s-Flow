from __future__ import annotations

import hashlib
import json

from .model import REALESRGAN_GENERATION_PROFILE, REALESRGAN_MODEL_NAME


def realesrgan_application_job_id(
    *,
    shot_id: int,
    source_sha256: str,
    source_width: int,
    source_height: int,
    source_frame_count: int,
    fps: int,
    target_width: int,
    target_height: int,
    model_name: str = REALESRGAN_MODEL_NAME,
    generation_profile: str = REALESRGAN_GENERATION_PROFILE,
    tile: int = 0,
    tile_pad: int = 10,
    pre_pad: int = 0,
    fp32: bool = False,
    encoder: str = "libx264",
    crf: int = 12,
    preset: str = "medium",
    pixel_format: str = "yuv420p",
) -> str:
    """Return the stable application job ID for one spatial-only 2x upscale."""

    plan = {
        "shot_id": shot_id,
        "source_sha256": source_sha256.lower(),
        "source_width": source_width,
        "source_height": source_height,
        "source_frame_count": source_frame_count,
        "fps": fps,
        "target_width": target_width,
        "target_height": target_height,
        "model_name": model_name,
        "generation_profile": generation_profile,
        "tile": tile,
        "tile_pad": tile_pad,
        "pre_pad": pre_pad,
        "fp32": fp32,
        "encoder": encoder,
        "crf": crf,
        "preset": preset,
        "pixel_format": pixel_format,
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"phase8-upscale-{shot_id:03d}-{digest[:16]}"
