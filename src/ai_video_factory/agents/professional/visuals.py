"""Minimal visual-only model output: Python owns immutable beat IDs and text.

A whole-video model choice refers to the validated B1.2 beats by ID, but
never retypes narration, ID source boundaries, or narrative beat taxonomy.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ai_video_factory.agents.professional.beats import GroupCuts
from ai_video_factory.bots.contracts import StrictModel, VisualType


class VisualTrigger(StrictModel):
    text: str
    occurrence: int = Field(ge=1)


class VisualEditorNote(StrictModel):
    kind: str
    instruction: str | None
    trigger: VisualTrigger | None


class VisualChoice(StrictModel):
    beat_id: str
    visual_type: VisualType
    prompt: str | None
    video_prompt: str | None
    reference: bool
    audio: bool
    image_prompts: list[str] | None
    generation_mode: Literal["grid", "independent"] | None
    editor_notes: list[VisualEditorNote]
    cta_group_id: int | None
    cta_reference_sources: list[Literal["presenter", "ebook"]] | None


class VisualGroup(StrictModel):
    block_id: int
    beats: list[VisualChoice] = Field(min_length=1)


class WholeVideoVisuals(StrictModel):
    blocks: list[VisualGroup] = Field(min_length=1)


class VisualAlignmentRepair(StrictModel):
    """Small replacement for ALL mismatched group visual entries in ONE call."""

    blocks: list[VisualGroup] = Field(min_length=1)


def canonicalize_visual_order(
    source_beats: list[dict[str, Any]],
    visuals: WholeVideoVisuals,
) -> WholeVideoVisuals:
    """Match uniquely keyed visual decisions to source IDs, without AI calls.

    Reordering keyed output objects is *not* beat resegmentation or a rhythm
    decision. If any ID is missing, duplicated or extra, keep the original
    response for explicit model repair instead.
    """
    group_ids = [x.block_id for x in visuals.blocks]
    expected_ids = [x["block_id"] for x in source_beats]
    if len(set(group_ids)) != len(group_ids) or set(group_ids) != set(expected_ids):
        return visuals
    lookup = {x.block_id: x for x in visuals.blocks}
    ordered: list[VisualGroup] = []
    for source in source_beats:
        group = lookup[source["block_id"]]
        selected_ids = [x.beat_id for x in group.beats]
        wanted_ids = [x["beat_id"] for x in source["beats"]]
        if len(set(selected_ids)) != len(selected_ids) or set(selected_ids) != set(wanted_ids):
            return visuals
        beats = {x.beat_id: x for x in group.beats}
        ordered.append(VisualGroup(
            block_id=source["block_id"],
            beats=[beats[i] for i in wanted_ids],
        ))
    return WholeVideoVisuals(blocks=ordered)


def mismatched_visual_blocks(
    source_beats: list[dict[str, Any]],
    visuals: WholeVideoVisuals,
) -> list[int]:
    """Identify incomplete or misordered group choices without touching rhythm."""
    available = {b.block_id: b for b in visuals.blocks}
    duplicates = len(available) != len(visuals.blocks)
    known_ids = {s["block_id"] for s in source_beats}
    if duplicates or set(available) - known_ids:
        return sorted(known_ids)
    return [
        source["block_id"]
        for source in source_beats
        if source["block_id"] not in available
        or [b.beat_id for b in available[source["block_id"]].beats]
        != [b["beat_id"] for b in source["beats"]]
    ]


def merge_alignment_repair(
    source_beats: list[dict[str, Any]],
    initial: WholeVideoVisuals,
    repaired: VisualAlignmentRepair,
    affected: list[int],
) -> WholeVideoVisuals:
    """Replace ONLY requested source groups, verify no hallucinated group IDs."""
    required = set(affected)
    returned = {block.block_id for block in repaired.blocks}
    if len(returned) != len(repaired.blocks) or returned != required:
        raise ValueError(
            "Visual correction must return each mismatched group exactly once."
        )
    all_choices = {
        group.block_id: group
        for group in initial.blocks
        if group.block_id not in required
    }
    all_choices.update({group.block_id: group for group in repaired.blocks})
    result = WholeVideoVisuals(blocks=[
        all_choices[source["block_id"]] for source in source_beats
    ])
    if mismatched_visual_blocks(source_beats, result):
        raise ValueError("Visual alignment still does not match canonical beat IDs.")
    return result


class VisualCorrection(StrictModel):
    """Only the changes, not a complete expensive reserialization of the video.

    If a block must be resegmented, return its new cuts AND a complete visual
    replacement for precisely that block. Every other block remains frozen.
    """

    resegmented_blocks: list[GroupCuts]
    replanned_blocks: list[VisualGroup]
    visual_patches: list[VisualChoice]


def assemble_plan(
    *,
    narrative_core: dict[str, str],
    source_beats: list[dict[str, Any]],
    visuals: WholeVideoVisuals | VisualCorrection,
) -> dict[str, Any]:
    """Reject missing, reordered and surplus beat decisions; never infer them."""
    if len(source_beats) != len(visuals.blocks):
        raise ValueError(
            "VISUAL_BLOCK_COUNT: visual output must contain every narrative block."
        )
    blocks: list[dict[str, Any]] = []
    for source, visual in zip(source_beats, visuals.blocks, strict=True):
        block_id = source["block_id"]
        if block_id != visual.block_id:
            raise ValueError(
                f"VISUAL_BLOCK_ID: expected block {block_id}, "
                f"got {visual.block_id}."
            )
        expected = source["beats"]
        if len(expected) != len(visual.beats):
            raise ValueError(
                f"VISUAL_BEAT_COUNT: block {block_id} expected {len(expected)} "
                f"visual decisions, got {len(visual.beats)}."
            )
        assembled: list[dict[str, Any]] = []
        for beat, decision in zip(expected, visual.beats, strict=True):
            if beat["beat_id"] != decision.beat_id:
                raise ValueError(
                    f"VISUAL_BEAT_ORDER: expected {beat['beat_id']}, "
                    f"got {decision.beat_id}."
                )
            selection = decision.model_dump(mode="json")
            # Required nullable fields stay explicit in the B2 carrier.
            for field in ("image_prompts", "generation_mode", "cta_group_id",
                          "cta_reference_sources"):
                if selection[field] is None:
                    selection.pop(field)
            assembled.append({
                **beat,
                **selection,
            })
        blocks.append({"block_id": block_id, "beats": assembled})
    return {
        "visual_style": "profesional",
        "narrative_core": narrative_core,
        "blocks": blocks,
    }



def apply_correction(
    *,
    current_plan: dict[str, Any],
    source_beats: list[dict[str, Any]],
    correction: VisualCorrection,
) -> dict[str, Any]:
    """Patch only named visual beats and resegmented groups, fail closed.

    This does NOT generate a new rhythm, invent missing images or auto-split
    narration: all changes come from explicit model choices. Python only joins
    validated source slices and checks reference integrity.
    """
    old = {b["block_id"]: b for b in current_plan["blocks"]}
    replaced = {g.block_id: g for g in correction.replanned_blocks}
    reseg = {g.block_id for g in correction.resegmented_blocks}
    if len(replaced) != len(correction.replanned_blocks):
        raise ValueError("Duplicate re-planned group.")
    if reseg != set(replaced):
        raise ValueError(
            "Every resegmented group must have a full visual replacement; "
            "all other groups must use targeted visual_patches."
        )
    patches = {v.beat_id: v for v in correction.visual_patches}
    if len(patches) != len(correction.visual_patches):
        raise ValueError("Duplicate visual patches.")
    retained_ids = {
        beat["beat_id"] for block in source_beats if block["block_id"] not in reseg
        for beat in block["beats"]
    }
    if not set(patches).issubset(retained_ids):
        raise ValueError("A visual patch references a missing or resegmented beat.")
    updated: list[VisualGroup] = []
    for group in source_beats:
        block_id = group["block_id"]
        if block_id in replaced:
            updated.append(replaced[block_id])
            continue
        choices = []
        existing = old[block_id]
        if len(existing["beats"]) != len(group["beats"]):
            raise ValueError("Source beat boundaries changed without replacement.")
        for beat in existing["beats"]:
            beat_id = beat["beat_id"]
            if beat_id in patches:
                choices.append(patches[beat_id])
                continue
            source_choice = {
                key: beat.get(key)
                for key in VisualChoice.model_fields
            }
            choices.append(VisualChoice.model_validate(source_choice))
        updated.append(VisualGroup(block_id=block_id, beats=choices))
    return assemble_plan(
        narrative_core=current_plan["narrative_core"],
        source_beats=source_beats,
        visuals=WholeVideoVisuals(blocks=updated),
    )

def summarize_beats(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Model-visible immutable source beats, without full-source duplication."""
    return {"blocks": blocks}
