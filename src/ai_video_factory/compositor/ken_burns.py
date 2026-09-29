from __future__ import annotations

from collections.abc import Iterable

MOTION_VERSION = "media-image-scale-crop-v1"
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


def _progress_expression(duration_seconds: float, *, start_seconds: float = 0.0) -> str:
    if duration_seconds <= 0:
        raise ValueError("Ken Burns duration must be positive")
    elapsed = "t" if start_seconds == 0 else f"(t-{start_seconds:.9f})"
    return f"min(max({elapsed}/{duration_seconds:.9f},0),1)"


def _scale_crop_filter(
    zoom_expression: str,
    *,
    width: int,
    height: int,
    fps: int,
    materialize_fps_first: bool,
) -> str:
    cover = f"max({width}/iw\\,{height}/ih)"
    scaled_width = f"trunc((iw*{cover}*{zoom_expression})/2)*2"
    scaled_height = f"trunc((ih*{cover}*{zoom_expression})/2)*2"
    x_expression = (
        "max(0,min(in_w-out_w,"
        f"(in_w-out_w)*{ANCHOR_X:.6f}))"
    )
    y_expression = (
        "max(0,min(in_h-out_h,"
        f"(in_h-out_h)*{ANCHOR_Y:.6f}))"
    )
    filters = [
        f"scale=w='{scaled_width}':h='{scaled_height}':eval=frame",
        f"crop={width}:{height}:x='{x_expression}':y='{y_expression}'",
        "setsar=1",
    ]
    if materialize_fps_first:
        filters.insert(0, f"fps={fps}")
    else:
        filters.append(f"fps={fps}")
    filters.append("format=yuv420p")
    return ",".join(filters)


def build_clip_ken_burns_filter(
    *,
    width: int,
    height: int,
    fps: int,
    duration_seconds: float,
) -> str:
    zoom_start = compute_zoom_start(duration_seconds)
    progress = _progress_expression(duration_seconds)
    zoom = (
        f"({zoom_start:.6f}+({ZOOM_END:.6f}-{zoom_start:.6f})*{progress})"
    )
    return _scale_crop_filter(
        zoom,
        width=width,
        height=height,
        fps=fps,
        materialize_fps_first=False,
    )


def build_timeline_ken_burns_filter(
    intervals: Iterable[tuple[float, float]],
    *,
    width: int,
    height: int,
    fps: int,
) -> str:
    terms: list[str] = []
    for start_seconds, end_seconds in intervals:
        duration_seconds = end_seconds - start_seconds
        zoom_start = compute_zoom_start(duration_seconds)
        progress = _progress_expression(
            duration_seconds,
            start_seconds=start_seconds,
        )
        active = (
            f"(gte(t\\,{start_seconds:.9f})*lt(t\\,{end_seconds:.9f}))"
        )
        delta = (
            f"(({zoom_start:.6f}-1)+"
            f"({ZOOM_END:.6f}-{zoom_start:.6f})*{progress})"
        )
        terms.append(f"{active}*{delta}")

    if not terms:
        return f"fps={fps},format=yuv420p"

    zoom = "1+" + "+".join(f"({term})" for term in terms)
    return _scale_crop_filter(
        zoom,
        width=width,
        height=height,
        fps=fps,
        materialize_fps_first=True,
    )
