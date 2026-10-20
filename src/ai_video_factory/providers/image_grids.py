"""Plan one large image per B2 numbered sequence, before any paid image calls.

The numbered beats are protected: fillers come only from unnumbered, still
unassigned described beats. When there are not enough described beats to fill
a grid, the remaining cells are explicit black panels.
Each visual asset belongs to exactly one job; a reviewed beat may own two assets.
No panel is cropped or materialized as a separate image in this stage.
"""

from __future__ import annotations

from typing import Any

from .grid_numbering import MAX_GRID_SEQUENCE_PANELS, parse_grid_marker

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
        marker = parse_grid_marker(description.lstrip())
        if marker is None:
            descriptions.append(f"{number}. {description}")
        elif visual_style == "profesional":
            # Physical cell order replaces the logical letter/ordinal marker.
            descriptions.append(f"{number}. {marker.body}")
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
    """Partition continuity into physical grids without splitting beat subshots.

    Video panels keep native 1080p cells in 2x2 grids; still-only runs can use
    3x3 grids.
    """
    from functools import lru_cache

    units: list[list[dict[str, Any]]] = []
    for asset in run:
        if units and (units[-1][0]["block_id"], units[-1][0]["beat_id"]) == (
            asset["block_id"], asset["beat_id"],
        ):
            units[-1].append(asset)
        else:
            units.append([asset])

    video_present = any(
        asset["visual_type"] in {"media_video", "avatar"} for asset in run
    )
    max_panels = 4 if video_present else 9

    @lru_cache(None)
    def solve(index: int) -> tuple[tuple[int, ...], ...] | None:
        if index == len(units):
            return ()
        candidates: list[tuple[tuple[int, ...], ...]] = []
        count = 0
        for end in range(index + 1, len(units) + 1):
            count += len(units[end - 1])
            if count > max_panels:
                break
            if count < 2 and not video_present:
                continue
            tail = solve(end)
            if tail is not None:
                candidates.append((tuple(range(index, end)),) + tail)
        if not candidates:
            return None
        return min(
            candidates, key=lambda groups: (
                len(groups),
                sum(
                    sum(len(units[i]) for i in group) == 1
                    for group in groups
                ),
                tuple(-sum(len(units[i]) for i in group) for group in groups),
            ),
        )

    grouping = solve(0)
    if grouping is None:
        raise GridPlanError("Cannot split numbered continuity into 2–9-panel grids")
    return [[asset for i in group for asset in units[i]] for group in grouping]


def plan_image_jobs(
    plan: dict[str, Any], described: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Plan legacy consecutive grids and lettered nonconsecutive B2 grids."""
    visual_style = plan.get("visual_style", "casero")
    if visual_style not in {"casero", "profesional"}:
        raise GridPlanError("Unknown B2 visual style")

    def key(item: dict[str, Any]) -> tuple[int, str]:
        return item["block_id"], item.get("asset_id", item["beat_id"])

    described_by_beat: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in described:
        described_by_beat.setdefault((item["block_id"], item["beat_id"]), []).append(item)
    for items in described_by_beat.values():
        items.sort(key=lambda item: int(item.get("subshot_index", 1)))

    positions: dict[tuple[int, str], int] = {}
    logical_groups: list[tuple[list[dict[str, Any]], str | None, bool]] = []
    lettered_members: dict[tuple[int, str], list[tuple[dict[str, Any], int]]] = {}
    cursor = 0

    for block in plan["blocks"]:
        legacy_current: list[dict[str, Any]] = []
        previous_cta: int | None = None

        def finish_legacy() -> None:
            nonlocal legacy_current
            if legacy_current:
                if len(legacy_current) < 2:
                    raise GridPlanError(
                        f"Orphan numbered description at {legacy_current[0]['beat_id']}"
                    )
                logical_groups.append((legacy_current, None, True))
                legacy_current = []

        for beat in block["beats"]:
            if beat.get("cta_group_id") != previous_cta:
                finish_legacy()
            previous_cta = beat.get("cta_group_id")
            sources = described_by_beat.get((block["block_id"], beat["beat_id"]), [])
            if not sources:
                finish_legacy()
                cursor += 1
                continue
            for source in sources:
                source_key = key(source)
                if source_key in positions:
                    raise GridPlanError(f"Duplicate visual asset position: {source_key[1]}")
                positions[source_key] = cursor
                cursor += 1
                marker = parse_grid_marker(source.get("description", ""))
                if source.get("generation") == "independent":
                    finish_legacy()
                    if marker is not None and marker.style == "lettered":
                        raise GridPlanError(
                            f"Lettered B2 grid asset {source['beat_id']} cannot use "
                            "independent generation"
                        )
                    continue
                if marker is None:
                    finish_legacy()
                    continue
                if marker.style == "lettered":
                    finish_legacy()
                    if visual_style != "profesional":
                        raise GridPlanError("Lettered grids are supported only for professional B2")
                    if marker.sequence_id is None:
                        raise GridPlanError("Lettered grid marker has no sequence ID")
                    lettered_members.setdefault(
                        (block["block_id"], marker.sequence_id), []
                    ).append((source, marker.panel_number))
                    continue

                number = marker.panel_number
                if number == 1:
                    finish_legacy()
                    legacy_current = [source]
                elif legacy_current and number == len(legacy_current) + 1:
                    legacy_current.append(source)
                else:
                    raise GridPlanError(
                        f"Invalid B2 continuity numbering at {source['beat_id']}: "
                        f"expected {len(legacy_current) + 1 if legacy_current else 1}, got {number}"
                    )
                if len(legacy_current) == (
                    MAX_GRID_SEQUENCE_PANELS if visual_style == "profesional" else 9
                ):
                    finish_legacy()
        finish_legacy()

    orphan_keys = set()
    for (block_id, label), members in lettered_members.items():
        members.sort(key=lambda item: positions[key(item[0])])
        numbers = [number for _asset, number in members]
        expected = list(range(1, len(members) + 1))
        if len(members) < 2:
            # A lone lettered panel has no grid to join; it is generated as a standalone image.
            orphan_keys.update(key(asset) for asset, _number in members)
            continue
        if len(members) > MAX_GRID_SEQUENCE_PANELS:
            raise GridPlanError(
                f"Lettered B2 grid {label} in block {block_id} has {len(members)} panels; "
                f"the maximum is {MAX_GRID_SEQUENCE_PANELS}, so continue the development "
                "under a new letter"
            )
        if numbers != expected:
            details = ", ".join(
                f"{asset['beat_id']}={label}{number}" for asset, number in members
            )
            raise GridPlanError(
                f"Lettered B2 grid {label} in block {block_id} must number its panels "
                f"1, 2, ..., {len(members)} in narrative order without restarting; "
                f"got [{details}]. "
                "A new development in the same block needs the next unused letter"
            )
        cta_ids = {asset.get("cta_group_id") for asset, _number in members}
        if len(cta_ids) > 1 or any(cta_id is not None for cta_id in cta_ids):
            raise GridPlanError(
                f"Lettered B2 grid {label} crosses a CTA ownership boundary"
            )
        logical_groups.append(([asset for asset, _number in members], label, False))

    logical_groups.sort(key=lambda item: positions[key(item[0][0])])
    standalone_numbered: set[tuple[int, str]] = set()
    standalone_numbered.update(orphan_keys)
    grid_groups: list[tuple[list[dict[str, Any]], str | None, bool]] = []
    for assets, label, allow_fillers in logical_groups:
        avatar_media_count = sum(asset["visual_type"] == "avatar_media" for asset in assets)
        if 0 < avatar_media_count < len(assets):
            standalone_numbered.update(key(asset) for asset in assets)
        else:
            grid_groups.append((assets, label, allow_fillers))

    numbered = {key(asset) for assets, _label, _fill in grid_groups for asset in assets}
    forced_independent = {
        key(asset) for asset in described if asset.get("generation") == "independent"
    }
    standalone_keys = standalone_numbered | forced_independent
    described_by_key = {key(asset): asset for asset in described}
    standalone = {asset_key: described_by_key[asset_key] for asset_key in standalone_keys}
    available = {
        key(asset): asset for asset in described
        if key(asset) not in numbered and key(asset) not in standalone_keys
    }

    grids: list[dict[str, Any]] = []
    for logical_run, label, allow_fillers in grid_groups:
        chunks = (
            _professional_grid_chunks(logical_run)
            if visual_style == "profesional" else [logical_run]
        )
        for chain_index, run in enumerate(chunks):
            columns, rows = (2, 2) if len(run) == 1 else _SHAPES[len(run)]
            count = columns * rows
            avatar_media_grid = all(asset["visual_type"] == "avatar_media" for asset in run)
            first = positions[key(run[0])]
            last = positions[key(run[-1])]
            use_fillers = allow_fillers and len(chunks) == 1
            forward = sorted(
                (
                    asset_key for asset_key in available
                    if positions[asset_key] > last
                    and available[asset_key].get("cta_group_id") == run[0].get("cta_group_id")
                    and (available[asset_key]["visual_type"] == "avatar_media") == avatar_media_grid
                ), key=positions.__getitem__,
            )[:count - len(run)] if use_fillers else []
            missing = count - len(run) - len(forward)
            backward = sorted(
                (
                    asset_key for asset_key in available
                    if positions[asset_key] < first
                    and available[asset_key].get("cta_group_id") == run[0].get("cta_group_id")
                    and (available[asset_key]["visual_type"] == "avatar_media") == avatar_media_grid
                ), key=positions.__getitem__, reverse=True,
            )[:missing] if use_fillers else []
            selected = [available[asset_key] for asset_key in forward + backward] + run
            for asset_key in forward + backward:
                del available[asset_key]
            selected.sort(key=lambda asset: positions[key(asset)])

            has_video = any(asset["visual_type"] in {"media_video", "avatar"} for asset in selected)
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
                    "beat_id": f"{run[0]['block_id']}BLACKPANEL{chr(ord('A') + index)}",
                    "asset_id": f"{run[0]['block_id']}BLACKPANEL{chr(ord('A') + index)}",
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
            chain_id = (
                f"{anchor['block_id']}:{label}"
                if label is not None else
                f"{logical_run[0]['block_id']}:"
                f"{logical_run[0].get('asset_id', logical_run[0]['beat_id'])}"
            )
            grids.append({
                "continuity_chain_id": chain_id,
                **({"continuity_sequence_id": label} if label is not None else {}),
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
                    "width": columns * panel_width, "height": rows * panel_height,
                    "numbered_beat_ids": [asset["beat_id"] for asset in run],
                    "numbered_asset_ids": [
                        asset.get("asset_id", asset["beat_id"]) for asset in run
                    ],
                    **({"sequence_id": label} if label is not None else {}),
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
                if (
                    visual_style == "profesional"
                    and asset["visual_type"] in {"media_video", "avatar"}
                )
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
    if plan.get("flow") == "titulo":
        # A T2 logical continuity world spans *multiple* B2 blocks. Existing
        # B2 uses block-local lettered labels; preserve visual memory by making
        # the first image of each later block depend on that world's latest
        # finished image, using the ordinary verified image-reference chain.
        latest_by_world: dict[str, dict[str, Any]] = {}
        for job in jobs:
            members = job.get("panels", [job])
            worlds = {
                panel["title_global_group"]
                for panel in members
                if panel.get("title_global_group") is not None
            }
            if len(worlds) != 1:
                continue
            world = next(iter(worlds))
            job["title_global_group"] = world
            previous = latest_by_world.get(world)
            if (
                previous is not None
                and previous["block_id"] != job["block_id"]
                and job.get("continuity_previous_asset_id") is None
            ):
                job["continuity_previous_asset_id"] = previous.get(
                    "asset_id", previous["beat_id"],
                )
                job["continuity_previous_block_id"] = previous["block_id"]
            latest_by_world[world] = job
    return jobs
