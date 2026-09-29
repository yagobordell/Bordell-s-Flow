"""Plan one large image per B2 numbered sequence, before any paid image calls.

The numbered beats are protected: fillers come only from unnumbered, still
unassigned described beats. When there are not enough described beats to fill
a grid, the remaining cells are explicit black panels. Each source beat
belongs to exactly one job. No panel is cropped or materialized as a separate
image in this stage.
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
GRID_PHOTO_HEADER = "iPhone 6 photograph done by an elderly:\n\n"
BLACK_PANEL_SOURCE = "black_panel"
BLACK_PANEL_DESCRIPTION = "black panel"


class GridPlanError(ValueError):
    """B2 numbering or grid descriptions cannot make a valid grid."""


def _grid_prompt(panels: list[dict[str, Any]], columns: int, rows: int) -> str:
    """Build the grid prompt with visual descriptions and narration context."""
    descriptions: list[str] = []
    voice_over: list[str] = []
    for number, panel in enumerate(panels, 1):
        description = panel["description"].strip()
        if _NUMBERED.match(description.lstrip()) is None:
            description = f"{number}. {description}"
        descriptions.append(description)
        text = panel.get("text")
        if panel.get("source") != BLACK_PANEL_SOURCE and isinstance(text, str) and text.strip():
            voice_over.append(f"{panel['beat_id']}: {text.strip()}")
    prompt = (
        GRID_PHOTO_HEADER
        + f"{columns}x{rows} grid 16:9\n\n"
        + "\n".join(descriptions)
        + "\n\nVoice over context:\n"
        + "\n".join(voice_over)
        + "\n\nUse the thinnest possible straight white separator lines between all grid cells"
        + "\n\nDo not add text to the image."
    )
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
        selected = [available[key] for key in forward + backward] + run
        for key in forward + backward:
            del available[key]
        selected.sort(key=lambda b: positions[b["block_id"], b["beat_id"]])
        black_panel_count = count - len(selected)
        selected.extend(
            {
                "block_id": run[0]["block_id"],
                "beat_id": (
                    f"{run[0]['block_id']}BLACKPANEL"
                    f"{chr(ord('A') + index)}"
                ),
                "visual_type": "media_image",
                "description": BLACK_PANEL_DESCRIPTION,
                "source": BLACK_PANEL_SOURCE,
            }
            for index in range(black_panel_count)
        )
        panels = [
            {
                **beat,
                "row": index // columns,
                "column": index % columns,
                "source": beat.get("source") or (
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

    def job_position(job: dict[str, Any]) -> int:
        if job.get("grid") is None:
            return positions[job["block_id"], job["beat_id"]]
        return min(
            positions[panel["block_id"], panel["beat_id"]]
            for panel in job["panels"]
            if panel.get("source") != BLACK_PANEL_SOURCE
        )

    jobs.sort(key=job_position)
    return jobs
