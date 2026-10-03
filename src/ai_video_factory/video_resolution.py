"""Public video-delivery presets; internal LTX uses grid-aligned padding.

New B2 and standalone Phase 8 jobs target Full HD. Previously submitted
720p Postgres jobs retain their explicit request dimensions and job hashes.
Salad's isolated 720p batch2 trial has its own deliberately fixed contract.
"""

from __future__ import annotations

DEFAULT_VIDEO_WIDTH = 1920
DEFAULT_VIDEO_HEIGHT = 1080
LEGACY_VIDEO_WIDTH = 1280
LEGACY_VIDEO_HEIGHT = 720
VIDEO_FPS = 24
APPROVED_VIDEO_SIZES = (
    (LEGACY_VIDEO_WIDTH, LEGACY_VIDEO_HEIGHT),
    (DEFAULT_VIDEO_WIDTH, DEFAULT_VIDEO_HEIGHT),
)


def validate_video_size(width: int, height: int, fps: int = VIDEO_FPS) -> None:
    """Reject untested public dimensions before queuing expensive GPU jobs."""
    if (width, height) not in APPROVED_VIDEO_SIZES or fps != VIDEO_FPS:
        raise ValueError(
            "Video delivery must be 1280x720 or 1920x1080 at 24 fps; "
            "the 1080p LTX output uses internal 1920x1088 padding and center crop"
        )
