from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

MOTION_VERSION = "media-image-subpixel-affine-v3"
ZOOM_SPEED_PER_SECOND = 0.02
DEFAULT_ANCHOR = "bottom_left"
ANCHOR_X = 0.02
ANCHOR_Y = 0.98

MotionDirection = Literal["in", "out"]


def motion_direction_for_index(index: int) -> MotionDirection:
    """Alternate still-image motion deterministically without affecting timing."""
    if index < 0:
        raise ValueError("Ken Burns motion index must be nonnegative")
    return "out" if index % 2 == 0 else "in"


def compute_zoom_bounds(
    duration_seconds: float,
    *,
    direction: MotionDirection,
) -> tuple[float, float]:
    if duration_seconds <= 0:
        raise ValueError("Ken Burns duration must be positive")
    zoom_delta = duration_seconds * ZOOM_SPEED_PER_SECOND
    if direction == "out":
        return 1.0 + zoom_delta, 1.0
    if direction == "in":
        return 1.0, 1.0 + zoom_delta
    raise ValueError(f"Unsupported Ken Burns direction: {direction}")


def compute_zoom_start(duration_seconds: float) -> float:
    """Backward-compatible zoom-out start used by older callers/tests."""
    return compute_zoom_bounds(duration_seconds, direction="out")[0]


def motion_metadata(
    duration_seconds: float,
    *,
    direction: MotionDirection = "out",
) -> dict[str, float | str]:
    zoom_start, zoom_end = compute_zoom_bounds(
        duration_seconds,
        direction=direction,
    )
    return {
        "version": MOTION_VERSION,
        "mode": "ken_burns",
        "direction": direction,
        "origin": DEFAULT_ANCHOR,
        "zoom_start": zoom_start,
        "zoom_end": zoom_end,
    }


def _progress_expression(frames: int, *, start_frame: int = 0) -> str:
    if frames <= 0 or start_frame < 0:
        raise ValueError("Ken Burns needs a positive frame count and nonnegative start")
    # FFmpeg perspective's 'on' counter is one-based. Reach the exact target
    # zoom on the LAST rendered frame, not one frame after the beat has ended.
    elapsed = "(on-1)" if start_frame == 0 else f"(on-{start_frame + 1})"
    return f"min(max({elapsed}/{max(1, frames - 1)}\\,0)\\,1)"


def _subpixel_filter(
    zoom_expression: str, *, width: int, height: int, fps: int
) -> str:
    if min(width, height, fps) <= 0:
        raise ValueError("Ken Burns geometry and fps must be positive")
    # Map fractional SOURCE corners onto the fixed-size destination. Unlike
    # scale(eval=frame)+crop, this never rounds a different width and height
    # or jumps the crop origin to a different integer/chroma sample.
    x_margin = f"(W-W/({zoom_expression}))"
    y_margin = f"(H-H/({zoom_expression}))"
    left = f"({x_margin}*{ANCHOR_X:.6f})"
    right = f"(W-{x_margin}*{1 - ANCHOR_X:.6f})"
    top = f"({y_margin}*{ANCHOR_Y:.6f})"
    bottom = f"(H-{y_margin}*{1 - ANCHOR_Y:.6f})"
    corners = (
        ("x0", left), ("y0", top),
        ("x1", right), ("y1", top),
        ("x2", left), ("y2", bottom),
        ("x3", right), ("y3", bottom),
    )
    perspective = "perspective=" + ":".join(
        [*(f"{name}='{expression}'" for name, expression in corners),
         "sense=source", "interpolation=linear", "eval=frame"]
    )
    return ",".join((
        f"fps={fps}",
        f"scale={width}:{height}:flags=lanczos",
        "format=yuv444p",
        perspective,
        "setsar=1",
        "format=yuv420p",
    ))


def build_clip_ken_burns_filter(
    *,
    width: int,
    height: int,
    fps: int,
    duration_seconds: float,
    direction: MotionDirection = "out",
) -> str:
    if min(width, height, fps) <= 0 or duration_seconds <= 0:
        raise ValueError("Ken Burns clip geometry, fps and duration must be positive")
    frames = round(duration_seconds * fps)
    if frames <= 0:
        raise ValueError("Ken Burns clip must contain at least one frame")
    zoom_start, zoom_end = compute_zoom_bounds(
        frames / fps,
        direction=direction,
    )
    progress = _progress_expression(frames)
    zoom = f"({zoom_start:.6f}+({zoom_end:.6f}-{zoom_start:.6f})*{progress})"
    return _subpixel_filter(zoom, width=width, height=height, fps=fps)


def build_timeline_ken_burns_filter(
    intervals: Iterable[tuple[int, int, MotionDirection]],
    *,
    width: int,
    height: int,
    fps: int,
) -> str:
    """Animate exact half-open FRAME ranges in the preview's 30-fps clock."""
    if min(width, height, fps) <= 0:
        raise ValueError("Ken Burns geometry and fps must be positive")
    terms: list[str] = []
    for start_frame, end_frame, direction in intervals:
        frames = end_frame - start_frame
        if start_frame < 0 or frames < 2:
            raise ValueError("Ken Burns preview interval must contain at least two frames")
        zoom_start, zoom_end = compute_zoom_bounds(
            frames / fps,
            direction=direction,
        )
        progress = _progress_expression(frames, start_frame=start_frame)
        # The perspective counter 'on' starts at 1, unlike the timeline's
        # zero-based frame indices. Non-media beats retain an identity transform.
        active = f"(gte(on\\,{start_frame + 1})*lt(on\\,{end_frame + 1}))"
        start_delta = zoom_start - 1.0
        end_delta = zoom_end - 1.0
        terms.append(
            f"({active}*({start_delta:.6f}+"
            f"({end_delta:.6f}-{start_delta:.6f})*{progress}))"
        )

    if not terms:
        return f"fps={fps},format=yuv420p"
    zoom = "1+" + "+".join(terms)
    return _subpixel_filter(zoom, width=width, height=height, fps=fps)
