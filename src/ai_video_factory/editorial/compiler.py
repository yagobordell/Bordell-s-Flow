"""Compile editorial intentions into deterministic frame-accurate render events."""
from __future__ import annotations

from typing import Any

from ai_video_factory.compositor.static_preview import _spoken_tokens
from ai_video_factory.editorial.contracts import E1Output, E2Output, SpokenAnchor
from ai_video_factory.editorial.timeline import RENDER_FPS, rendered_windows


class EditorialValidationError(ValueError):
    pass


def resolve_anchor(anchor: SpokenAnchor, index: dict[str, list[dict[str, Any]]]) -> float:
    words = index.get(anchor.beat_id)
    if words is None:
        raise EditorialValidationError(f"Unknown beat in editorial anchor: {anchor.beat_id}")
    phrase = _spoken_tokens(anchor.phrase)
    if not phrase:
        raise EditorialValidationError("Empty spoken anchor")
    matches = [
        pos for pos in range(len(words) - len(phrase) + 1)
        if [item["token"] for item in words[pos:pos + len(phrase)]] == phrase
    ]
    if anchor.occurrence > len(matches):
        raise EditorialValidationError(f"Unresolved spoken anchor in {anchor.beat_id}")
    start = words[matches[anchor.occurrence - 1]]["start_seconds"]
    if start is None:
        raise EditorialValidationError("Speech aligner could not time the requested phrase")
    return float(start)


def compile_plan(
    e1: E1Output, e2: E2Output, index: dict[str, list[dict[str, Any]]],
    timeline: dict[str, Any],
) -> dict[str, Any]:
    # The original STT/preview timeline is 30 fps, while the authoritative
    # assembled LTX video is 24 fps. Beat source frame fields remain immutable.
    windows = rendered_windows(timeline)
    fps = RENDER_FPS
    total_frames = round(float(timeline["duration_seconds"]) * fps)
    expected = {item.id for item in e1.interventions}
    decisions = {item.id: item for item in e2.decisions}
    if len(e2.decisions) != len(decisions) or expected != set(decisions):
        raise EditorialValidationError("E2 decision IDs differ from E1")
    order = {beat["beat_id"]: position for position, beat in enumerate(timeline["beats"])}
    events = []
    for effect in e1.interventions:
        if effect.end_beat_id not in windows:
            raise EditorialValidationError("Editorial end beat does not exist")
        times = []
        indices = []
        for element in effect.elements:
            moment = resolve_anchor(element.anchor, index)
            times.append(moment)
            indices.append(order[element.anchor.beat_id])
        if times != sorted(times) or order[effect.end_beat_id] < max(indices):
            raise EditorialValidationError("Editorial event sequence is not chronological")
        end_frame = windows[effect.end_beat_id][1]
        start_frame = round(times[0] * fps)
        if start_frame < 0 or end_frame > total_frames or end_frame <= start_frame:
            raise EditorialValidationError("Editorial event escapes speech duration")
        decision = decisions[effect.id]
        if decision.status == "suppressed":
            continue
        frames = [round(t * fps) for t in times]
        if any(frame < start_frame or frame >= end_frame for frame in frames):
            raise EditorialValidationError("Editorial element is outside its interval")
        layout = decision.model_dump(exclude={"id", "status", "reason"})
        events.append({
            "id": effect.id, "kind": effect.kind,
            "start_frame": start_frame, "end_frame": end_frame,
            "elements": [
                {"text": el.text, "frame": frame}
                for el, frame in zip(effect.elements, frames, strict=True)
            ],
            "animation": effect.animation, "layout": layout,
        })
    return {
        "schema_version": "editorial-render-v1",
        "fps": fps, "width": 1920, "height": 1080,
        "duration_frames": total_frames, "events": events,
    }
