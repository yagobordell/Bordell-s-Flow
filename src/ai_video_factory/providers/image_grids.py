"""Plan one large image per B2 numbered sequence, before any paid image calls.

The numbered beats are protected: fillers come only from unnumbered, still
unassigned described beats. Each source beat belongs to exactly one job.
No panel is cropped or materialized as a separate image in this stage.
"""

from __future__ import annotations

import re
from typing import Any

_NUMBERED = re.compile(r"^([1-9])\.\s+(.+)$", re.DOTALL)
_SHAPES = {
    2: (2, 2), 3: (2, 2), 4: (2, 2),
    5: (3, 2), 6: (3, 2),
    7: (3, 3), 8: (3, 3), 9: (3, 3),
}
_PANEL_WIDTH = 1280
_PANEL_HEIGHT = 720


class GridPlanError(ValueError):
    """B2 numbering or available filler beats cannot make a valid grid."""


def _grid_prompt(panels: list[dict[str, Any]], columns: int, rows: int) -> str:
    """Describe independent full-resolution panels without changing B2 output."""
    instructions = [
        f"Generate ONE single {columns}-column by {rows}-row contact sheet, "
        f"exactly {len(panels)} equal panels, each 16:9.",
        "Read panels left to right, then top to bottom. Every panel must be a "
        "separate faithful photographable frame for its description. Maintain "
        "the same identities, objects, surroundings and physical continuity "
        "where the descriptions are numbered. No merged scenes, no omitted or "
        "repeated panel, no borders, gutters, captions, panel numbers or text. "
        "Fill the entire canvas with these panels.",
    ]
    for number, panel in enumerate(panels, 1):
        description = panel["description"]
        match = _NUMBERED.match(description.lstrip())
        if match:
            description = match.group(2)
        instructions.append(f"Panel {number} (row {panel['row'] + 1}, "
                            f"column {panel['column'] + 1}): {description}")
    prompt = "\n".join(instructions)
    if len(prompt) + 400 > 32_000:
        raise GridPlanError("Grid prompt exceeds the supported GPT Image prompt limit")
    return prompt


def plan_image_jobs(
    plan: dict[str, Any], described: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group 2-9 consecutive numbered beats, filling forward then backward.

    Numbering belongs to each original B2 block. Fillers may come from other
    blocks, but only if not numbered and not already assigned to another grid.
    The output ordering remains the original script ordering.
    """
    positions: dict[tuple[int, str], int] = {}
    runs: list[list[dict[str, Any]]] = []
    described_by_key = {(b["block_id"], b["beat_id"]): b for b in described}
    cursor = 0
    for block in plan["blocks"]:
        current: list[dict[str, Any]] = []

        def finish() -> None:
            nonlocal current
            if current:
                if len(current) < 2:
                    raise GridPlanError(
                        f"Orphan numbered description at {current[0]['beat_id']}"
                    )
                runs.append(current)
                current = []

        for beat in block["beats"]:
            key = block["block_id"], beat["beat_id"]
            positions[key] = cursor
            cursor += 1
            source = described_by_key.get(key)
            if source is None:
                finish()
                continue
            matched = _NUMBERED.match(source["description"].lstrip())
            if matched is None:
                finish()
                continue
            number = int(matched.group(1))
            if number == 1:
                finish()
                current = [source]
            elif current and number == len(current) + 1:
                current.append(source)
            else:
                raise GridPlanError(
                    f"Invalid B2 continuity numbering at {source['beat_id']}: "
                    f"expected {len(current) + 1 if current else 1}, got {number}"
                )
            if len(current) == 9:
                finish()
        finish()

    numbered = {
        (beat["block_id"], beat["beat_id"])
        for run in runs for beat in run
    }
    available = {
        (beat["block_id"], beat["beat_id"]): beat
        for beat in described
        if (beat["block_id"], beat["beat_id"]) not in numbered
    }
    grids: list[dict[str, Any]] = []
    for run in runs:
        columns, rows = _SHAPES[len(run)]
        count = columns * rows
        first = positions[run[0]["block_id"], run[0]["beat_id"]]
        last = positions[run[-1]["block_id"], run[-1]["beat_id"]]
        forward = sorted(
            (key for key in available if positions[key] > last),
            key=positions.__getitem__,
        )[:count - len(run)]
        missing = count - len(run) - len(forward)
        backward = sorted(
            (key for key in available if positions[key] < first),
            key=positions.__getitem__,
            reverse=True,
        )[:missing]
        if len(forward) + len(backward) != count - len(run):
            raise GridPlanError(
                f"Grid beginning at {run[0]['beat_id']} requires {count} "
                "distinct described beats; insufficient free images for padding"
            )
        selected = [available[key] for key in forward + backward] + run
        for key in forward + backward:
            del available[key]
        selected.sort(key=lambda b: positions[b["block_id"], b["beat_id"]])
        panels = [
            {
                **beat,
                "row": index // columns,
                "column": index % columns,
                "source": (
                    "numbered"
                    if (beat["block_id"], beat["beat_id"]) in numbered
                    else "filler"
                ),
            }
            for index, beat in enumerate(selected)
        ]
        anchor = run[0]
        grids.append({
            "kind": "grid",
            "block_id": anchor["block_id"],
            "beat_id": anchor["beat_id"],
            "visual_type": anchor["visual_type"],
            "description": _grid_prompt(panels, columns, rows),
            "size": f"{columns * _PANEL_WIDTH}x{rows * _PANEL_HEIGHT}",
            "grid": {
                "columns": columns, "rows": rows,
                "panel_width": _PANEL_WIDTH, "panel_height": _PANEL_HEIGHT,
                "width": columns * _PANEL_WIDTH,
                "height": rows * _PANEL_HEIGHT,
                "numbered_beat_ids": [beat["beat_id"] for beat in run],
            },
            "panels": panels,
        })
    jobs = list(available.values()) + grids
    jobs.sort(key=lambda job: min(
        positions[beat["block_id"], beat["beat_id"]]
        for beat in job.get("panels", [job])
    ))
    return jobs
