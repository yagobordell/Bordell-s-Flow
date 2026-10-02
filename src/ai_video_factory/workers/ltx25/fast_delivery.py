"""Opt-in 576p fast-path preparation and exact-contract 720p delivery.

The original landscape profile remains bit-for-bit untouched. Fast keyframes
preserve the full original FOV (including 1280x736 Qwen keyframes). The final
scale is 16:9 -> 16:9 with no cropping and exactly one audio-free output.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from PIL import Image

FAST_INTERNAL_SIZE = (1024, 576)
FAST_DELIVERY_SIZE = (1280, 720)


def prepare_fast_keyframe(source: Path, destination: Path) -> Path:
    """Downsample the full 16:9 content; edge-extend any non-16:9 Qwen input."""
    # Import lazily: the existing full-resolution code does not depend on the
    # experimental adapter, and this reuses its proven no-crop fitting method.
    from .model import _fit_image_with_edge_padding

    with Image.open(source) as opened:
        image = opened.convert("RGB")
    if image.width <= 0 or image.height <= 0:
        raise ValueError("The fast-path input keyframe must not be empty")
    fitted = _fit_image_with_edge_padding(
        image, width=FAST_INTERNAL_SIZE[0], height=FAST_INTERNAL_SIZE[1]
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fitted.save(destination, format="PNG")
    return destination


def deliver_fast_video(
    source: Path, destination: Path, *, timeout_seconds: int = 300
) -> float:
    """Scale completed raw 576p MP4 via ffmpeg, atomically publish silent 720p.

    Keep the raw clip when ffmpeg fails; do not upload a partial delivery file.
    """
    if source == destination or not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Fast-path source must be a nonempty separate MP4")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".delivery.tmp.mp4")
    temporary.unlink(missing_ok=True)
    started = time.monotonic()
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-y", "-i", str(source),
        "-map", "0:v:0", "-an",
        "-vf", "scale=1280:720:flags=lanczos,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(temporary),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False,
            timeout=timeout_seconds,
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError(
                "Fast-path 720p delivery failed; raw 576p retained. "
                + result.stderr[-1500:]
            )
        os.replace(temporary, destination)
        return time.monotonic() - started
    finally:
        temporary.unlink(missing_ok=True)
