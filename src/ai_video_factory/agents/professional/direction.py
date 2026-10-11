"""One global editorial decision: source cuts AND visuals for every beat.

The LLM proposes narrative boundaries, shot carriers, grids, visual prompts and
editor notes in the SAME structured object and the SAME OpenAI request.
Python then materializes the exact original narration and validates the
complete audiovisual proposal, never splitting rhythm from visual direction.
"""
from __future__ import annotations

from typing import Any

from pydantic import Field

from ai_video_factory.agents.professional.beats import (
    BeatCut,
    GroupCuts,
    WholeScriptCuts,
    validate_and_materialize_cuts,
)
from ai_video_factory.agents.professional.visuals import (
    VisualChoice,
    VisualGroup,
    WholeVideoVisuals,
    assemble_plan,
    canonicalize_visual_order,
    mismatched_visual_blocks,
)
from ai_video_factory.bots.contracts import BeatType, StrictModel


class DirectedBeat(VisualChoice):
    """A single editorial choice binds a cut to its filming decision."""

    end_anchor: str = Field(min_length=1)
    occurrence: int = Field(ge=1)
    beat_type: BeatType


class DirectedGroup(StrictModel):
    block_id: int
    beats: list[DirectedBeat] = Field(min_length=1)


class WholeVideoDirection(StrictModel):
    blocks: list[DirectedGroup] = Field(min_length=1)


class DirectionCorrection(StrictModel):
    """One coherent correction for the entire video, not per-group AI calls.

    Replaced groups carry BOTH new cuts and new visuals together; visual-only
    patches preserve their original immutable narration and beat identity.
    """

    replaced_groups: list[DirectedGroup]
    visual_patches: list[VisualChoice]


def direction_cuts(direction: WholeVideoDirection) -> WholeScriptCuts:
    return WholeScriptCuts(blocks=[
        GroupCuts(
            block_id=group.block_id,
            cuts=[
                BeatCut(
                    end_anchor=beat.end_anchor,
                    occurrence=beat.occurrence,
                    beat_type=beat.beat_type,
                )
                for beat in group.beats
            ],
        )
        for group in direction.blocks
    ])


def direction_visuals(direction: WholeVideoDirection) -> WholeVideoVisuals:
    return WholeVideoVisuals(blocks=[
        VisualGroup(
            block_id=group.block_id,
            beats=[
                VisualChoice.model_validate({
                    key: value for key, value in beat.model_dump(mode="json").items()
                    if key in VisualChoice.model_fields
                })
                for beat in group.beats
            ],
        )
        for group in direction.blocks
    ])


def materialize_direction(
    groups: list[dict[str, Any]],
    direction: WholeVideoDirection,
    core: dict[str, Any],
) -> dict[str, Any]:
    """Create a full candidate only if all narrated cuts and IDs are valid.

    A rejected integrated response is still saved with ALL its visual decisions
    and its concrete issues; Python never discards visuals merely because an
    associated beat boundary is invalid.
    """
    cuts = direction_cuts(direction)
    boundaries = validate_and_materialize_cuts(groups, cuts)
    result: dict[str, Any] = {
        "status": "blocked",
        "issues": list(boundaries["issues"]),
        "boundaries": boundaries,
        "direction": direction.model_dump(mode="json"),
        "plan": None,
    }
    if boundaries["status"] == "blocked":
        return result
    source_beats = boundaries["blocks"]
    visuals = canonicalize_visual_order(source_beats, direction_visuals(direction))
    missing = mismatched_visual_blocks(source_beats, visuals)
    if missing:
        result["issues"].append({
            "rule_id": "VISUAL_BEAT_ALIGNMENT",
            "severity": "error",
            "message": "Every narrated beat must have its own corresponding visual.",
            "block_ids": missing,
        })
        return result
    try:
        result["plan"] = assemble_plan(
            narrative_core=core, source_beats=source_beats, visuals=visuals,
        )
    except (ValueError, TypeError) as exc:
        result["issues"].append({
            "rule_id": "VISUAL_ASSEMBLY", "severity": "error",
            "message": str(exc),
        })
        return result
    result["status"] = "passed"
    return result


def apply_direction_correction(
    direction: WholeVideoDirection,
    correction: DirectionCorrection,
) -> WholeVideoDirection:
    """Atomically apply specified audiovisual changes, reject all ambiguity."""
    original = {group.block_id: group for group in direction.blocks}
    if len(original) != len(direction.blocks):
        raise ValueError("Duplicate original narrative block IDs.")
    replacement = {group.block_id: group for group in correction.replaced_groups}
    if len(replacement) != len(correction.replaced_groups):
        raise ValueError("Duplicate replacement group IDs.")
    if not set(replacement).issubset(original):
        raise ValueError("A replacement refers to an unknown narrative group.")
    existing_beats = {
        beat.beat_id: (group.block_id, beat)
        for group in direction.blocks
        for beat in group.beats
    }
    if len(existing_beats) != sum(len(g.beats) for g in direction.blocks):
        raise ValueError("Duplicate original beat IDs.")
    changes = {beat.beat_id: beat for beat in correction.visual_patches}
    if len(changes) != len(correction.visual_patches):
        raise ValueError("Duplicate visual correction beat IDs.")
    if not set(changes).issubset(existing_beats):
        raise ValueError("A visual patch refers to an unknown beat.")
    if any(existing_beats[beat_id][0] in replacement for beat_id in changes):
        raise ValueError("Patch targets a group already fully replaced.")
    updated: list[DirectedGroup] = []
    for group in direction.blocks:
        if group.block_id in replacement:
            updated.append(replacement[group.block_id])
            continue
        beats: list[DirectedBeat] = []
        for beat in group.beats:
            patch = changes.get(beat.beat_id)
            if patch is None:
                beats.append(beat)
                continue
            values = beat.model_dump(mode="json")
            values.update(patch.model_dump(mode="json"))
            beats.append(DirectedBeat.model_validate(values))
        updated.append(DirectedGroup(block_id=group.block_id, beats=beats))
    return WholeVideoDirection(blocks=updated)
