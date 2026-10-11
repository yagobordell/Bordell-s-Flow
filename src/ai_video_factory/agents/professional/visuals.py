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


class VisualCorrection(StrictModel):
    """Empty cut replacement list means keep every existing narrated beat."""

    resegmented_blocks: list[GroupCuts]
    blocks: list[VisualGroup] = Field(min_length=1)


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


def summarize_beats(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Model-visible immutable source beats, without full-source duplication."""
    return {"blocks": blocks}
