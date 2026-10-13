from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_GRID_SEQUENCE_PANELS = 20

_LEGACY_MARKER = re.compile(r"^([1-9]|1[0-9]|20)\.\s+(.+)$", re.DOTALL)
_LETTERED_MARKER = re.compile(
    r"^([A-Z]+)([0-9]+)(?:\.\s+|\s+)(.+)$", re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class GridMarker:
    """A B2 logical-grid label and its ordered panel number."""

    style: str
    sequence_id: str | None
    panel_number: int
    body: str


def parse_grid_marker(description: object) -> GridMarker | None:
    """Parse a lettered ``A1 scene`` marker or legacy numeric ``1. scene``."""
    if not isinstance(description, str):
        return None
    value = description.lstrip()
    matched = _LETTERED_MARKER.match(value)
    if matched is not None:
        return GridMarker(
            style="lettered",
            sequence_id=matched.group(1).upper(),
            panel_number=int(matched.group(2)),
            body=matched.group(3),
        )
    matched = _LEGACY_MARKER.match(value)
    if matched is not None:
        return GridMarker(
            style="legacy", sequence_id=None,
            panel_number=int(matched.group(1)), body=matched.group(2),
        )
    return None


def format_grid_marker(
    style: str, sequence_id: str | None, panel_number: int, body: str,
) -> str:
    if style == "lettered":
        if not sequence_id or not sequence_id.isalpha() or not sequence_id.isupper():
            raise ValueError("Lettered grid markers require an uppercase sequence ID")
        return f"{sequence_id}{panel_number}. {body.strip()}"
    if style == "legacy":
        return f"{panel_number}. {body.strip()}"
    raise ValueError(f"Unknown grid marker style: {style}")


def alphabetic_sequence_id(index: int) -> str:
    """Return a one-based Excel-style uppercase label: A..Z, AA, AB, ..."""
    if index < 1:
        raise ValueError("Grid sequence IDs are one-based")
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _next_unused_sequence_id(used: set[str]) -> str:
    index = 1
    while True:
        candidate = alphabetic_sequence_id(index)
        if candidate not in used:
            return candidate
        index += 1


def reflow_lettered_groups_at_cta_boundaries(block: dict[str, Any]) -> None:
    """Keep lettered B2 grids whole while splitting any sequence around CTA beats."""
    beats = block.get("beats")
    if not isinstance(beats, list):
        return
    groups: dict[str, list[tuple[int, dict[str, Any], GridMarker]]] = {}
    for position, beat in enumerate(beats):
        marker = parse_grid_marker(beat.get("prompt"))
        if marker is None or marker.style != "lettered" or marker.sequence_id is None:
            continue
        groups.setdefault(marker.sequence_id, []).append((position, beat, marker))
    if not groups:
        return

    reserved_ids = set(groups)
    used_ids: set[str] = set()
    fragments: list[tuple[int, str, list[tuple[int, dict[str, Any], GridMarker]]]] = []
    for original_id, members in groups.items():
        ordered = sorted(members, key=lambda item: item[0])
        segment: list[tuple[int, dict[str, Any], GridMarker]] = []
        for member in ordered:
            position, beat, marker = member
            if beat.get("cta_group_id") is not None:
                beat["prompt"] = marker.body.strip()
                if segment:
                    fragments.append((segment[0][0], original_id, segment))
                    segment = []
                continue
            if segment and any(
                beats[index].get("cta_group_id") is not None
                for index in range(segment[-1][0] + 1, position)
            ):
                fragments.append((segment[0][0], original_id, segment))
                segment = []
            segment.append(member)
        if segment:
            fragments.append((segment[0][0], original_id, segment))

    fragments.sort(key=lambda item: item[0])
    first_fragment_for_id: set[str] = set()
    for _position, original_id, members in fragments:
        if len(members) < 2:
            for _beat_position, beat, marker in members:
                beat["prompt"] = marker.body.strip()
            continue
        if original_id not in first_fragment_for_id and original_id not in used_ids:
            new_id = original_id
            first_fragment_for_id.add(original_id)
        else:
            new_id = _next_unused_sequence_id(reserved_ids | used_ids)
        used_ids.add(new_id)
        for panel_number, (_beat_position, beat, marker) in enumerate(members, start=1):
            beat["prompt"] = format_grid_marker(
                "lettered", new_id, panel_number, marker.body,
            )
