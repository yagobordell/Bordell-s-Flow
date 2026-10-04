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
# avatar_media occupies half of a 1280x720 frame: 640x720 = 8:9.
# GPT Image 2.5 requires at least 655,360 pixels, so 768x864 is the
# smallest exact 8:9 output with both edges divisible by 16.
_AVATAR_MEDIA_PANEL_WIDTH = 768
_AVATAR_MEDIA_PANEL_HEIGHT = 864
GRID_PHOTO_HEADER = "iPhone 6 photograph done by an elderly:\n\n"
BLACK_PANEL_SOURCE = "black_panel"
BLACK_PANEL_DESCRIPTION = "black panel"


class GridPlanError(ValueError):
    """B2 numbering or grid descriptions cannot make a valid grid."""


def _grid_prompt(
    panels: list[dict[str, Any]], columns: int, rows: int,
    *, visual_style: str = "casero", panel_aspect: str = "16:9",
) -> str:
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
        (GRID_PHOTO_HEADER if visual_style == "casero" else "")
        + f"{columns}x{rows} grid {panel_aspect}\n\n"
        + "\n".join(descriptions)
        + "\n\nVoice over context:\n"
        + "\n".join(voice_over)
        + "\n\nUse the thinnest possible straight white separator lines between all grid cells"
        + (
            "\n\nDo not add text to the image."
            if visual_style == "casero"
            else (
                "\n\nKeep each panel faithful to its own scene description, "
                "including naturally occurring scene details and the specified "
                "camera viewpoint. Treat Voice over context as narrative context "
                "while the panel descriptions determine the visible scenes. "
                "For screens or documents, preserve each panel's chosen composition "
                "without adding a person or imposing a screen/page orientation. "
                "Specific app/page/document content in a panel's (For context: ...) "
                "note remains narrative context, not a visible text or layout request."
            )
        )
    )
    if visual_style == "profesional":
        prompt += (
            "\n\nRespect EACH panel's own requested shot scale, camera viewpoint, "
            "crop and visual focus. For continuous numbered steps, preserve "
            "the same subject and setting while maintaining deliberate "
            "shot-to-shot framing differences (e.g. medium, close-up, "
            "tight detail, alternate angle) from their individual prompts. "
            "Keep the resulting panels visually distinct while following "
            "each panel's specific described content and natural framing."
        )
        reference_panels = [str(i) for i, p in enumerate(panels, 1) if p.get("reference")]
        if reference_panels:
            prompt += ("\n\nThe attached avatar reference image may be used ONLY for panels "
                       + ", ".join(reference_panels)
                       + "; all other panels follow their own independent scene "
                       "descriptions and subjects.")
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
    visual_style = plan.get("visual_style", "casero")
    if visual_style not in {"casero", "profesional"}:
        raise GridPlanError("Unknown B2 visual style")
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

    grid_runs: list[list[dict[str, Any]]] = []
    standalone_numbered: set[tuple[int, str]] = set()
    for run in runs:
        avatar_media_count = sum(
            beat["visual_type"] == "avatar_media" for beat in run
        )
        if 0 < avatar_media_count < len(run):
            standalone_numbered.update(
                (beat["block_id"], beat["beat_id"]) for beat in run
            )
        else:
            grid_runs.append(run)

    numbered = {
        (beat["block_id"], beat["beat_id"])
        for run in grid_runs for beat in run
    }
    standalone = {
        key: described_by_key[key] for key in standalone_numbered
    }
    available = {
        (beat["block_id"], beat["beat_id"]): beat
        for beat in described
        if (
            (beat["block_id"], beat["beat_id"]) not in numbered
            and (beat["block_id"], beat["beat_id"]) not in standalone_numbered
        )
    }
    grids: list[dict[str, Any]] = []
    for run in grid_runs:
        columns, rows = _SHAPES[len(run)]
        count = columns * rows
        avatar_media_grid = all(
            beat["visual_type"] == "avatar_media" for beat in run
        )
        # Only 2x2 landscape grids can fit native 1920x1080 video panels
        # within the supported GPT canvas. Larger 3-column grids retain
        # 1280x720 panels; avatar_media grids keep their separate 8:9 ratio.
        has_video = any(
            beat["visual_type"] in {"media_video", "avatar"} for beat in run
        ) or any(
            available[key]["visual_type"] in {"media_video", "avatar"}
            for key in forward + backward
        )
        panel_width, panel_height = (
            (_AVATAR_MEDIA_PANEL_WIDTH, _AVATAR_MEDIA_PANEL_HEIGHT)
            if avatar_media_grid else (1920, 1080)
            if visual_style == "profesional" and has_video and (columns, rows) == (2, 2)
            else (_PANEL_WIDTH, _PANEL_HEIGHT)
        )

        first = positions[run[0]["block_id"], run[0]["beat_id"]]
        last = positions[run[-1]["block_id"], run[-1]["beat_id"]]
        forward = sorted(
            (
                key for key in available
                if (
                    positions[key] > last
                    and (
                        available[key]["visual_type"] == "avatar_media"
                    ) == avatar_media_grid
                )
            ),
            key=positions.__getitem__,
        )[:count - len(run)]
        missing = count - len(run) - len(forward)
        backward = sorted(
            (
                key for key in available
                if (
                    positions[key] < first
                    and (
                        available[key]["visual_type"] == "avatar_media"
                    ) == avatar_media_grid
                )
            ),
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
                "reference": False,
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
            "description": _grid_prompt(
                panels, columns, rows, visual_style=visual_style,
                panel_aspect="8:9" if avatar_media_grid else "16:9",
            ),
            **({"visual_style": visual_style} if visual_style == "profesional" else {}),
            "size": f"{columns * panel_width}x{rows * panel_height}",
            "grid": {
                "columns": columns, "rows": rows,
                "panel_width": panel_width, "panel_height": panel_height,
                "width": columns * panel_width,
                "height": rows * panel_height,
                "numbered_beat_ids": [beat["beat_id"] for beat in run],
            },
            "panels": panels,
            **({"video_panel_ids": [
                p["beat_id"] for p in panels
                if p.get("source") != BLACK_PANEL_SOURCE
                and p["visual_type"] in {"media_video", "avatar"}
            ]} if visual_style == "profesional" else {}),
        })
    individuals = [*available.values(), *standalone.values()]
    jobs = [
        {
            **beat,
            **(
                {"size": f"{_AVATAR_MEDIA_PANEL_WIDTH}x{_AVATAR_MEDIA_PANEL_HEIGHT}"}
                if beat["visual_type"] == "avatar_media"
                else {"size": "1920x1088", "video_source": True}
                if visual_style == "profesional"
                and beat["visual_type"] in {"media_video", "avatar"}
                else {}
            ),
        }
        for beat in individuals
    ] + grids

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
