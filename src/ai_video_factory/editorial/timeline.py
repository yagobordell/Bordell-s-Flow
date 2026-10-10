"""Resolve the final 24-fps LTX clock from the saved STT/preview timeline.

The original STT timeline runs at 30 fps, while the final assembled LTX MP4
is 24 fps. Never reinterpret 30-fps beat frame numbers as rendered frames.
"""
from __future__ import annotations

import math
from typing import Any

RENDER_FPS = 24


class EditorialTimelineError(ValueError):
    pass


def rendered_windows(timeline: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """Get immutable beat intervals on the actual LTX compositor's 24-fps clock."""
    source_fps = timeline.get("fps")
    if (
        isinstance(source_fps, bool)
        or not isinstance(source_fps, int)
        or source_fps not in (24, 30)
    ):
        raise EditorialTimelineError("Unsupported source STT timeline frame rate")

    duration = timeline.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise EditorialTimelineError("Invalid source STT timeline duration")

    beats = timeline.get("beats")
    if not isinstance(beats, list) or not beats:
        raise EditorialTimelineError("Missing source STT timeline beats")

    windows: dict[str, tuple[int, int]] = {}
    previous_end = 0
    for beat in beats:
        if not isinstance(beat, dict):
            raise EditorialTimelineError("Malformed source STT beat")
        beat_id = beat.get("beat_id")
        if not isinstance(beat_id, str) or not beat_id or beat_id in windows:
            raise EditorialTimelineError("Invalid or repeated STT beat ID")

        start_seconds = beat.get("start_seconds")
        end_seconds = beat.get("end_seconds")
        if start_seconds is not None and end_seconds is not None:
            if (
                isinstance(start_seconds, bool) or isinstance(end_seconds, bool)
                or not isinstance(start_seconds, (int, float))
                or not isinstance(end_seconds, (int, float))
                or not math.isfinite(start_seconds)
                or not math.isfinite(end_seconds)
                or start_seconds < 0 or end_seconds <= start_seconds
            ):
                raise EditorialTimelineError("Invalid STT beat time window")
            start = round(start_seconds * RENDER_FPS)
            end = round(end_seconds * RENDER_FPS)
        elif source_fps == RENDER_FPS:
            # Supports older saved 24-fps records and compact unit fixtures.
            start = beat.get("start_frame")
            end = beat.get("end_frame")
        else:
            raise EditorialTimelineError(
                "30-fps STT beats require seconds to derive 24-fps render boundaries"
            )

        if (
            isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, int) or not isinstance(end, int)
            or start != previous_end or end <= start
        ):
            raise EditorialTimelineError(
                f"Invalid or discontinuous 24-fps render window for {beat_id}"
            )

        # The LTX compositor writes these additional 24-fps fields next to
        # the original 30-fps preview frame fields. Cross-check them.
        for key, actual in (("render_start_frame", start), ("render_end_frame", end)):
            saved = beat.get(key)
            if saved is not None and (
                isinstance(saved, bool) or not isinstance(saved, int)
                or saved != actual
            ):
                raise EditorialTimelineError(
                    f"Saved LTX {key} differs from 24-fps time boundary for {beat_id}"
                )
        windows[beat_id] = (start, end)
        previous_end = end

    if previous_end != round(duration * RENDER_FPS):
        raise EditorialTimelineError("24-fps windows differ from video duration")
    return windows
