from __future__ import annotations

from collections.abc import Iterable

MOTION_VERSION = "media-image-subpixel-affine-v2"
ZOOM_SPEED_PER_SECOND = 0.02
ZOOM_END = 1.0
DEFAULT_ANCHOR = "bottom_left"
ANCHOR_X = 0.02
ANCHOR_Y = 0.98


def compute_zoom_start(duration_seconds: float) -> float:
    if duration_seconds <= 0:
        raise ValueError("Ken Burns duration must be positive")
    return 1.0 + duration_seconds * ZOOM_SPEED_PER_SECOND


def motion_metadata(duration_seconds: float) -> dict[str, float | str]:
    return {
        "version": MOTION_VERSION,
        "mode": "ken_burns",
        "direction": "out",
        "origin": DEFAULT_ANCHOR,
        "zoom_start": compute_zoom_start(duration_seconds),
        "zoom_end": ZOOM_END,
    }


def _progress_expression(frames: int, *, start_frame: int = 0) -> str:
    if frames <= 0 or start_frame < 0:
        raise ValueError("Ken Burns needs a positive frame count and nonnegative start")
    # FFmpeg perspective's 'on' counter is one-based. Reach exactly zoom=1
    # on the LAST rendered frame, not one frame after the beat has ended.
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
) -> str:
    if min(width, height, fps) <= 0 or duration_seconds <= 0:
        raise ValueError("Ken Burns clip geometry, fps and duration must be positive")
    frames = round(duration_seconds * fps)
    if frames <= 0:
        raise ValueError("Ken Burns clip must contain at least one frame")
    zoom_start = compute_zoom_start(frames / fps)
    progress = _progress_expression(frames)
    zoom = f"({zoom_start:.6f}+({ZOOM_END:.6f}-{zoom_start:.6f})*{progress})"
    return _subpixel_filter(zoom, width=width, height=height, fps=fps)


def build_timeline_ken_burns_filter(
    intervals: Iterable[tuple[int, int]],
    *,
    width: int,
    height: int,
    fps: int,
) -> str:
    """Animate exact half-open FRAME ranges in the preview's 30-fps clock."""
    if min(width, height, fps) <= 0:
        raise ValueError("Ken Burns geometry and fps must be positive")
    terms: list[str] = []
    for start_frame, end_frame in intervals:
        frames = end_frame - start_frame
        if start_frame < 0 or frames < 2:
            raise ValueError("Ken Burns preview interval must contain at least two frames")
        zoom_start = compute_zoom_start(frames / fps)
        progress = _progress_expression(frames, start_frame=start_frame)
        # The perspective counter 'on' starts at 1, unlike the timeline's
        # zero-based frame indices. Non-media beats retain an identity transform.
        active = f"(gte(on\\,{start_frame + 1})*lt(on\\,{end_frame + 1}))"
        terms.append(f"({active}*({zoom_start - 1:.6f}*(1-{progress})))")

    if not terms:
        return f"fps={fps},format=yuv420p"
    zoom = "1+" + "+".join(terms)
    return _subpixel_filter(zoom, width=width, height=height, fps=fps)
