"""Physical image jobs for Agent B's cross-block A1/A2/B1/B2/A3 logical sequences.

A label identifies a recurring visual storyline, NEVER a physical panel position.
Only neighboring chronological assets may occupy the same paid grid; a sequence
may later resume, referencing an already generated image rather than redrawing it.
The legacy numerical B2 grid planner is deliberately left unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from .image_grids import (
    _AVATAR_MEDIA_PANEL_HEIGHT,
    _AVATAR_MEDIA_PANEL_WIDTH,
    _PANEL_HEIGHT,
    _PANEL_WIDTH,
    _SHAPES,
    BLACK_PANEL_DESCRIPTION,
    BLACK_PANEL_SOURCE,
    GridPlanError,
    _grid_prompt,
    _professional_grid_chunks,
)

_LOGICAL = re.compile(r"^([A-Z]{1,4})([1-9][0-9]*)\.\s+(.+)$", re.DOTALL)
_LOCAL = re.compile(r"^[1-9][0-9]*\.\s+")


def _key(asset: dict[str, Any]) -> tuple[int, str]:
    return asset["block_id"], asset.get("asset_id", asset["beat_id"])


def _label(asset: dict[str, Any]) -> tuple[str, int] | None:
    desc = asset.get("description", "")
    if not isinstance(desc, str):
        raise GridPlanError("Invalid Agent B visual description")
    match = _LOGICAL.match(desc.lstrip())
    if match:
        return match.group(1), int(match.group(2))
    if _LOCAL.match(desc.lstrip()):
        raise GridPlanError("Agent B requires A1/A2 logical labels, not old numeric panels")
    return None


def _compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("cta_group_id") == right.get("cta_group_id")
        and (left["visual_type"] == "avatar_media")
        == (right["visual_type"] == "avatar_media")
        and left.get("cta_reference_sources") == right.get("cta_reference_sources")
        and left.get("generation") != "independent"
        and right.get("generation") != "independent"
    )


def _job_for_run(
    assets: list[dict[str, Any]], chain: str, index: int,
) -> dict[str, Any]:
    if not 2 <= len(assets) <= 4:
        raise GridPlanError("Physical Agent B grids must contain 2–4 original assets")
    columns, rows = _SHAPES[len(assets)]
    avatar_media = all(a["visual_type"] == "avatar_media" for a in assets)
    has_video = any(a["visual_type"] == "media_video" for a in assets)
    panel_width, panel_height = (
        (_AVATAR_MEDIA_PANEL_WIDTH, _AVATAR_MEDIA_PANEL_HEIGHT) if avatar_media
        else (1920, 1080) if has_video and (columns, rows) == (2, 2)
        else (_PANEL_WIDTH, _PANEL_HEIGHT)
    )
    first = assets[0]
    panels: list[dict[str, Any]] = []
    for offset, asset in enumerate(assets):
        panels.append({**asset, "row": offset // columns, "column": offset % columns,
                       "source": "numbered"})
    for i in range(columns * rows - len(assets)):
        black_id = f"{first['block_id']}BLACKPANEL{index}_{i}"
        position = len(panels)
        panels.append({
            "block_id": first["block_id"],
            "beat_id": black_id,
            "asset_id": black_id,
            "visual_type": "media_image",
            "description": BLACK_PANEL_DESCRIPTION,
            "source": BLACK_PANEL_SOURCE,
            "reference": False,
            "row": position // columns,
            "column": position % columns,
        })
    anchor = assets[0]
    return {
        "kind": "grid",
        "block_id": anchor["block_id"],
        "beat_id": anchor["beat_id"],
        "asset_id": anchor.get("asset_id", anchor["beat_id"]),
        "visual_type": anchor["visual_type"],
        "description": _grid_prompt(
            panels, columns, rows, visual_style="profesional",
            panel_aspect="8:9" if avatar_media else "16:9",
            logical_labels=True,
        ),
        "visual_style": "profesional",
        "logical_sequence_id": chain,
        "continuity_chain_id": chain,
        "continuity_index": index,
        "size": f"{columns * panel_width}x{rows * panel_height}",
        "grid": {
            "columns": columns, "rows": rows,
            "panel_width": panel_width, "panel_height": panel_height,
            "width": columns * panel_width, "height": rows * panel_height,
            "numbered_beat_ids": [a["beat_id"] for a in assets],
            "numbered_asset_ids": [a.get("asset_id", a["beat_id"]) for a in assets],
        },
        "panels": panels,
        "video_panel_ids": [
            a["beat_id"] for a in assets if a["visual_type"] == "media_video"
        ],
    }


def plan_agent_b_image_jobs(
    plan: dict[str, Any], described: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return physical jobs with chronological cross-block PNG dependencies.

    A1 A2 B1 B2 A3 -> grid(A1,A2), grid(B1,B2), individual(A3 -> first A grid).
    No grid ever stretches across the intervening B scene or an unrelated asset.
    """
    if plan.get("visual_style") != "profesional":
        raise GridPlanError("Agent B grids require professional visual style")
    seen: set[tuple[int, str]] = set()
    last_ordinals: dict[str, int] = {}
    labels: list[tuple[str, int] | None] = []
    for asset in described:
        key = _key(asset)
        if key in seen:
            raise GridPlanError(f"Duplicate Agent B asset: {key}")
        seen.add(key)
        label = _label(asset)
        if label:
            chain, ordinal = label
            expected = last_ordinals.get(chain, 0) + 1
            if ordinal != expected:
                raise GridPlanError(
                    f"Logical sequence {chain}: expected {chain}{expected}, got {chain}{ordinal}"
                )
            last_ordinals[chain] = ordinal
        labels.append(label)
    # Each sequence may be segmented over distant B1.1 blocks, but its
    # next ordinal must refer to earlier chronological visuals.
    jobs: list[dict[str, Any]] = []
    latest_job: dict[str, Any] = {}
    indexes: dict[str, int] = {}
    i = 0
    while i < len(described):
        start = described[i]
        label = labels[i]
        if label is None or start.get("generation") == "independent":
            segment = [start]
            chain = label[0] if label else None
            end = i + 1
            groups = [segment]
        else:
            chain = label[0]
            end = i + 1
            while (
                end < len(described)
                and labels[end] is not None
                and labels[end][0] == chain
                and _compatible(described[end - 1], described[end])
            ):
                end += 1
            segment = described[i:end]
            if len(segment) == 1:
                groups = [segment]
            else:
                groups = _professional_grid_chunks(segment)
        for group in groups:
            logical = chain if chain is not None else None
            index = indexes.get(logical, 0) if logical else 0
            job = (
                _job_for_run(group, logical, index)
                if len(group) >= 2 else {
                    **group[0],
                    **(
                        {"size": f"{_AVATAR_MEDIA_PANEL_WIDTH}x{_AVATAR_MEDIA_PANEL_HEIGHT}"}
                        if group[0]["visual_type"] == "avatar_media"
                        else {"size": "1920x1088", "video_source": True}
                        if group[0]["visual_type"] == "media_video"
                        else {}
                    ),
                }
            )
            if logical:
                job["logical_sequence_id"] = logical
                job["continuity_chain_id"] = logical
                job["continuity_index"] = index
                if logical in latest_job:
                    prior = latest_job[logical]
                    job["continuity_previous_block_id"] = prior["block_id"]
                    job["continuity_previous_asset_id"] = prior.get(
                        "asset_id", prior["beat_id"]
                    )
                latest_job[logical] = job
                indexes[logical] = index + 1
            jobs.append(job)
        i = end
    return jobs
