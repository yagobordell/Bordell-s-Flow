"""Plan one large image per B2 numbered sequence, before any paid image calls.

The numbered beats are protected: fillers come only from unnumbered, still
unassigned described beats. When there are not enough described beats to fill
a grid, the remaining cells are explicit black panels.
Each visual asset belongs to exactly one job; a reviewed beat may own two assets.
No panel is cropped or materialized as a separate image in this stage.
"""

from __future__ import annotations

import re
from typing import Any

_NUMBERED = re.compile(r"^([1-9]|1[0-8])\.\s+(.+)$", re.DOTALL)
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
        numbered = _NUMBERED.match(description.lstrip())
        if numbered is None:
            descriptions.append(f"{number}. {description}")
        elif visual_style == "profesional":
            # A backward filler can precede the numbered sequence in the grid.
            descriptions.append(f"{number}. {numbered.group(2)}")
        else:
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
        presenter_panels = [
            str(i) for i, panel in enumerate(panels, 1)
            if panel.get("reference") and (
                panel.get("cta_reference_sources") is None
                or "presenter" in panel["cta_reference_sources"]
            )
        ]
        ebook_panels = [
            str(i) for i, panel in enumerate(panels, 1)
            if "ebook" in (panel.get("cta_reference_sources") or [])
        ]
        if presenter_panels:
            prompt += (
                "\n\nThe attached presenter identity reference may be used ONLY for panels "
                + ", ".join(presenter_panels)
                + " ONLY. Preserve recognizable identity where needed, but never "
                "copy the presenter's original narration room into a generated scene."
            )
        if ebook_panels:
            prompt += (
                "\n\nThe attached authentic ebook-cover reference applies to panels "
                + ", ".join(ebook_panels)
                + " ONLY. Preserve the actual cover when it appears in a panel; "
                "do not force an ebook to be visible solely because it is attached."
            )
    if len(prompt) + 400 > 32_000:
        raise GridPlanError("Grid prompt exceeds the supported GPT Image prompt limit")
    return prompt


def _professional_grid_chunks(
    run: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Partition logical continuity into 2–4-cell grids without splitting beat subshots."""
    from functools import lru_cache

    units: list[list[dict[str, Any]]] = []
    for asset in run:
        if units and (units[-1][0]["block_id"], units[-1][0]["beat_id"]) == (
            asset["block_id"], asset["beat_id"],
        ):
            units[-1].append(asset)
        else:
            units.append([asset])

    @lru_cache(None)
    def solve(index: int) -> tuple[tuple[int, ...], ...] | None:
        if index == len(units):
            return ()
        candidates: list[tuple[tuple[int, ...], ...]] = []
        count = 0
        for end in range(index + 1, len(units) + 1):
            count += len(units[end - 1])
            if count > 4:
                break
            if count < 2:
                continue
            tail = solve(end)
            if tail is not None:
                candidates.append((tuple(range(index, end)),) + tail)
        if not candidates:
            return None
        return min(
            candidates, key=lambda groups: (
                len(groups),
                tuple(-sum(len(units[i]) for i in group) for group in groups),
            ),
        )

    grouping = solve(0)
    if grouping is None:
        raise GridPlanError("Cannot split numbered continuity into 2–4-panel grids")
    return [[asset for i in group for asset in units[i]] for group in grouping]


def plan_image_jobs(
    plan: dict[str, Any], described: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group logical numbered visual assets, then plan 2–4 cell grids.

    A beat may now own two image assets. Asset identity, not parent beat identity,
    determines generation state and grid position. Parent beat_id is retained for
    narration/timing downstream.
    """
    visual_style = plan.get("visual_style", "casero")
    if visual_style not in {"casero", "profesional"}:
        raise GridPlanError("Unknown B2 visual style")

    def key(item: dict[str, Any]) -> tuple[int, str]:
        return item["block_id"], item.get("asset_id", item["beat_id"])

    described_by_beat: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in described:
        described_by_beat.setdefault(
            (item["block_id"], item["beat_id"]), []
        ).append(item)
    for items in described_by_beat.values():
        items.sort(key=lambda item: int(item.get("subshot_index", 1)))

    positions: dict[tuple[int, str], int] = {}
    runs: list[list[dict[str, Any]]] = []
    cursor = 0
    for block in plan["blocks"]:
        current: list[dict[str, Any]] = []
        previous_cta: int | None = None

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
            if beat.get("cta_group_id") != previous_cta:
                finish()
            previous_cta = beat.get("cta_group_id")
            sources = described_by_beat.get(
                (block["block_id"], beat["beat_id"]), []
            )
            if not sources:
                finish()
                cursor += 1
                continue
            for source in sources:
                source_key = key(source)
                if source_key in positions:
                    raise GridPlanError(
                        f"Duplicate visual asset position: {source_key[1]}"
                    )
                positions[source_key] = cursor
                cursor += 1
                if source.get("generation") == "independent":
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
                if len(current) == (18 if visual_style == "profesional" else 9):
                    finish()
        finish()

    grid_runs: list[list[dict[str, Any]]] = []
    standalone_numbered: set[tuple[int, str]] = set()
    for run in runs:
        avatar_media_count = sum(
            asset["visual_type"] == "avatar_media" for asset in run
        )
        if 0 < avatar_media_count < len(run):
            standalone_numbered.update(key(asset) for asset in run)
        else:
            grid_runs.append(run)

    numbered = {
        key(asset) for run in grid_runs for asset in run
    }
    forced_independent = {
        key(asset) for asset in described
        if asset.get("generation") == "independent"
    }
    standalone_keys = standalone_numbered | forced_independent
    described_by_key = {key(asset): asset for asset in described}
    standalone = {
        asset_key: described_by_key[asset_key] for asset_key in standalone_keys
    }
    available = {
        key(asset): asset
        for asset in described
        if key(asset) not in numbered and key(asset) not in standalone_keys
    }

    grids: list[dict[str, Any]] = []
    for logical_run in grid_runs:
        chunks = (
            _professional_grid_chunks(logical_run)
            if visual_style == "profesional" else [logical_run]
        )
        for chain_index, run in enumerate(chunks):
            columns, rows = _SHAPES[len(run)]
            count = columns * rows
            avatar_media_grid = all(
                asset["visual_type"] == "avatar_media" for asset in run
            )
            first = positions[key(run[0])]
            last = positions[key(run[-1])]
            allow_fillers = len(chunks) == 1
            forward = sorted(
                (
                    asset_key for asset_key in available
                    if (
                        positions[asset_key] > last
                        and available[asset_key].get("cta_group_id") == run[0].get("cta_group_id")
                        and (
                            available[asset_key]["visual_type"] == "avatar_media"
                        ) == avatar_media_grid
                    )
                ),
                key=positions.__getitem__,
            )[:count - len(run)] if allow_fillers else []
            missing = count - len(run) - len(forward)
            backward = sorted(
                (
                    asset_key for asset_key in available
                    if (
                        positions[asset_key] < first
                        and available[asset_key].get("cta_group_id") == run[0].get("cta_group_id")
                        and (
                            available[asset_key]["visual_type"] == "avatar_media"
                        ) == avatar_media_grid
                    )
                ),
                key=positions.__getitem__,
                reverse=True,
            )[:missing] if allow_fillers else []
            selected = [available[asset_key] for asset_key in forward + backward] + run
            for asset_key in forward + backward:
                del available[asset_key]
            selected.sort(key=lambda asset: positions[key(asset)])

            has_video = any(
                asset["visual_type"] in {"media_video", "avatar"} for asset in selected
            )
            panel_width, panel_height = (
                (_AVATAR_MEDIA_PANEL_WIDTH, _AVATAR_MEDIA_PANEL_HEIGHT)
                if avatar_media_grid else (1920, 1080)
                if visual_style == "profesional" and has_video and (columns, rows) == (2, 2)
                else (_PANEL_WIDTH, _PANEL_HEIGHT)
            )
            black_panel_count = count - len(selected)
            selected.extend(
                {
                    "block_id": run[0]["block_id"],
                    "beat_id": (
                        f"{run[0]['block_id']}BLACKPANEL"
                        f"{chr(ord('A') + index)}"
                    ),
                    "asset_id": (
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
                    **asset,
                    "row": index // columns,
                    "column": index % columns,
                    "source": asset.get("source") or (
                        "numbered" if key(asset) in numbered else "filler"
                    ),
                }
                for index, asset in enumerate(selected)
            ]
            anchor = run[0]
            grids.append({
                "continuity_chain_id": (
                    f"{logical_run[0]['block_id']}:"
                    f"{logical_run[0].get('asset_id', logical_run[0]['beat_id'])}"
                ),
                "continuity_index": chain_index,
                **({
                    "continuity_previous_asset_id": chunks[chain_index - 1][0].get(
                        "asset_id", chunks[chain_index - 1][0]["beat_id"],
                    ),
                } if chain_index else {}),
                "kind": "grid",
                "block_id": anchor["block_id"],
                "beat_id": anchor["beat_id"],
                "asset_id": anchor.get("asset_id", anchor["beat_id"]),
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
                    "numbered_beat_ids": [asset["beat_id"] for asset in run],
                    "numbered_asset_ids": [
                        asset.get("asset_id", asset["beat_id"]) for asset in run
                    ],
                },
                "panels": panels,
                **({"video_panel_ids": [
                    panel["beat_id"] for panel in panels
                    if panel.get("source") != BLACK_PANEL_SOURCE
                    and panel["visual_type"] in {"media_video", "avatar"}
                ]} if visual_style == "profesional" else {}),
            })

    individuals = [*available.values(), *standalone.values()]
    jobs = [
        {
            **asset,
            **(
                {"size": f"{_AVATAR_MEDIA_PANEL_WIDTH}x{_AVATAR_MEDIA_PANEL_HEIGHT}"}
                if asset["visual_type"] == "avatar_media"
                else {"size": "1920x1088", "video_source": True}
                if visual_style == "profesional"
                and asset["visual_type"] in {"media_video", "avatar"}
                else {}
            ),
        }
        for asset in individuals
    ] + grids

    def job_position(job: dict[str, Any]) -> int:
        if job.get("grid") is None:
            return positions[key(job)]
        return min(
            positions[key(panel)]
            for panel in job["panels"]
            if panel.get("source") != BLACK_PANEL_SOURCE
        )

    jobs.sort(key=job_position)
    return jobs
