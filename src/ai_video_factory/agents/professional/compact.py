"""Sparse, source-text-first OpenAI contract for ONE joint audiovisual plan.

Each beat carries only fields relevant to its visual carrier. Editor notes,
identity references and ebook CTA ownership are group-side annotations, never
a dozen null/empty fields on every beat. The existing B2 contracts remain
unchanged and are populated only after deterministic source verification.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, model_validator

from ai_video_factory.agents.professional.visuals import (
    VisualChoice,
    VisualGroup,
    WholeVideoVisuals,
    assemble_plan,
)
from ai_video_factory.bots.contracts import BeatType, StrictModel


class BeatBase(StrictModel):
    beat_id: str
    text: str = Field(min_length=1)
    beat_type: BeatType
    editor_notes: str = ""


class AvatarBeat(BeatBase):
    visual_type: Literal["avatar"]


class AvatarMediaBeat(BeatBase):
    visual_type: Literal["avatar_media"]
    prompt: str = Field(min_length=1)


class ImageBeat(BeatBase):
    visual_type: Literal["media_image"]
    image_prompts: list[str] = Field(min_length=1, max_length=4)
    generation_mode: Literal["grid", "independent"] | None = None


class VideoBeat(BeatBase):
    visual_type: Literal["media_video"]
    prompt: str = Field(min_length=1)
    video_prompt: str = Field(min_length=1)


SparseBeat = AvatarBeat | AvatarMediaBeat | ImageBeat | VideoBeat


class NoteTrigger(StrictModel):
    text: str
    occurrence: int = Field(ge=1)


class BeatNote(StrictModel):
    beat_id: str
    kind: str
    instruction: str
    trigger: NoteTrigger | None


class CTAAssignment(StrictModel):
    beat_id: str
    cta_group_id: int = Field(ge=1)
    sources: list[Literal["presenter", "ebook"]]


class SparseGroup(StrictModel):
    block_id: int
    beats: list[SparseBeat] = Field(min_length=1)
    presenter_references: list[str]
    cta: list[CTAAssignment]

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_editor_notes(cls, value: Any) -> Any:
        """Read historical group notes, but never expose them on the new wire."""
        if not isinstance(value, dict) or "editor_notes" not in value:
            return value
        data = dict(value)
        legacy_notes = data.pop("editor_notes")
        if not legacy_notes:
            return data
        if not isinstance(legacy_notes, list):
            raise ValueError("Legacy group editor_notes must be a list.")
        beats = [
            beat.model_dump(mode="json") if hasattr(beat, "model_dump") else dict(beat)
            for beat in data["beats"]
        ]
        by_id = {beat["beat_id"]: beat for beat in beats}
        for item in legacy_notes:
            note = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            if not isinstance(note, dict) or note.get("beat_id") not in by_id:
                raise ValueError("EDITOR_NOTE_BEAT: old editor note targets unknown beat.")
            instruction = note.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                raise ValueError("Legacy editor note must have an instruction string.")
            beat = by_id[note["beat_id"]]
            existing = beat.get("editor_notes", "")
            beat["editor_notes"] = (existing + " " + instruction).strip()
        data["beats"] = beats
        return data


class SparseDirection(StrictModel):
    blocks: list[SparseGroup] = Field(min_length=1)


class AvatarPatch(StrictModel):
    beat_id: str
    visual_type: Literal["avatar"]
    editor_notes: str | None = None


class AvatarMediaPatch(StrictModel):
    beat_id: str
    visual_type: Literal["avatar_media"]
    prompt: str = Field(min_length=1)
    editor_notes: str | None = None


class ImagePatch(StrictModel):
    beat_id: str
    visual_type: Literal["media_image"]
    image_prompts: list[str] = Field(min_length=1, max_length=4)
    generation_mode: Literal["grid", "independent"] | None = None
    editor_notes: str | None = None


class VideoPatch(StrictModel):
    beat_id: str
    visual_type: Literal["media_video"]
    prompt: str = Field(min_length=1)
    video_prompt: str = Field(min_length=1)
    editor_notes: str | None = None


SparseVisualPatch = AvatarPatch | AvatarMediaPatch | ImagePatch | VideoPatch


class SparseCorrection(StrictModel):
    replaced_groups: list[SparseGroup]
    visual_patches: list[SparseVisualPatch]


def _source_segments(
    source: str, beats: list[SparseBeat], block_id: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate model text against source; only source bytes reach production.

    Space/newline normalization by the LLM is accepted, but punctuation,
    words and order are NEVER rewritten or inferred. The original separators
    belong to the preceding beat, preserving the legacy source contract.
    """
    position = 0
    resolved: list[str] = []
    issues: list[dict[str, Any]] = []
    for beat in beats:
        tokens = beat.text.split()
        if not tokens:
            issues.append({
                "rule_id": "BEAT_TEXT", "severity": "error",
                "beat_ids": [beat.beat_id],
                "message": "Beat text is empty.",
            })
            break
        pattern = r"\s*" + r"\s+".join(re.escape(x) for x in tokens)
        match = re.match(pattern, source[position:])
        if match is None:
            issues.append({
                "rule_id": "SOURCE_TEXT", "severity": "error",
                "beat_ids": [beat.beat_id], "block_id": block_id,
                "message": (
                    "Beat narration is not the next exact source words. "
                    "Copy the original words and punctuation in order."
                ),
                "source_start": position,
            })
            break
        end = position + match.end()
        # A concatenated source can match even when the model splits a word:
        # "Ca" + "ll now." must never become two valid narrative beats.
        if (end < len(source) and end > position
                and source[end - 1].isalnum() and source[end].isalnum()):
            issues.append({
                "rule_id": "CUT_INSIDE_WORD", "severity": "error",
                "beat_ids": [beat.beat_id], "block_id": block_id,
                "message": "A beat cannot end inside a source word.",
                "source_start": end,
            })
            break
        while end < len(source) and source[end].isspace():
            end += 1
        resolved.append(source[position:end])
        position = end
    if not issues and position != len(source):
        issues.append({
            "rule_id": "SOURCE_TILING", "severity": "error",
            "block_id": block_id,
            "message": "Beats do not cover the complete source group.",
            "source_start": position, "source_end": len(source),
        })
    return resolved, issues


def materialize_sparse_direction(
    groups: list[dict[str, Any]],
    direction: SparseDirection,
    core: dict[str, Any],
) -> dict[str, Any]:
    """Build source-exact visuals even if beat lengths need editorial repair.

    Structural/source mismatches prevent assembly. A fully tiled source is
    assembled without legacy end anchors so downstream Python validators can
    report avatar runs, ratio, image splits, and genuine ebook CTA defects in
    the *same* round as beat length errors. No automatic cuts or rewrites.
    """
    issues: list[dict[str, Any]] = []
    boundary_issues: list[dict[str, Any]] = []
    source_blocks: list[dict[str, Any]] = []
    visual_blocks: list[VisualGroup] = []
    if len(groups) != len(direction.blocks):
        issues.append({
            "rule_id": "CUT_BLOCKS", "severity": "error",
            "message": "Every source group must be present exactly once.",
        })
    for source, group in zip(groups, direction.blocks, strict=False):
        block_id = source["block_id"]
        if group.block_id != block_id:
            issues.append({
                "rule_id": "CUT_BLOCK_ORDER", "severity": "error",
                "message": f"Expected block {block_id}, got {group.block_id}.",
            })
            continue
        original_texts, errors = _source_segments(
            source["text"], group.beats, block_id,
        )
        issues.extend(errors)
        if errors:
            continue
        ids = [beat.beat_id for beat in group.beats]
        expected = [
            f"{block_id}{_suffix(index)}"
            for index in range(1, len(group.beats) + 1)
        ]
        if ids != expected:
            issues.append({
                "rule_id": "BEAT_ID", "severity": "error",
                "block_id": block_id,
                "message": "Beat IDs must be sequential and unique.",
            })
            continue
        id_set = set(ids)
        if (len(set(group.presenter_references)) != len(group.presenter_references)
                or not set(group.presenter_references).issubset(id_set)):
            issues.append({
                "rule_id": "REFERENCE_BEAT", "severity": "error",
                "block_id": block_id,
                "message": "Unknown or duplicate presenter reference IDs.",
            })
            continue
        cta = {assignment.beat_id: assignment for assignment in group.cta}
        if (len(cta) != len(group.cta) or not set(cta).issubset(id_set)):
            issues.append({
                "rule_id": "CTA_BEAT", "severity": "error",
                "block_id": block_id,
                "message": "Unknown or duplicate CTA assignments.",
            })
            continue
        source_beats: list[dict[str, Any]] = []
        visual_choices: list[VisualChoice] = []
        position = 0
        for beat, original in zip(group.beats, original_texts, strict=True):
            length = len(original.strip())
            words = len(original.split())
            if length > 100 or words > 40:
                boundary_issues.append({
                    "rule_id": "BEAT_LENGTH", "severity": "error",
                    "beat_ids": [beat.beat_id], "block_id": block_id,
                    "message": (
                        f"Beat has {length} stripped characters and {words} words "
                        "(max 100 chars, 40 words). Choose semantic source cuts."
                    ),
                    "source_start": position,
                    "source_end": position + len(original),
                })
            position += len(original)
            source_beats.append({
                "beat_id": beat.beat_id, "text": original,
                "beat_type": beat.beat_type,
            })
            owner = cta.get(beat.beat_id)
            kind = beat.visual_type
            sources = list(owner.sources) if owner else None
            fields: dict[str, Any] = {
                "beat_id": beat.beat_id,
                "visual_type": kind,
                "prompt": getattr(beat, "prompt", None),
                "video_prompt": getattr(beat, "video_prompt", None),
                "reference": (
                    beat.beat_id in group.presenter_references or bool(sources)
                ),
                "audio": kind in ("avatar", "avatar_media"),
                "image_prompts": getattr(beat, "image_prompts", None),
                "generation_mode": getattr(beat, "generation_mode", None),
                "editor_notes": beat.editor_notes,
                "cta_group_id": owner.cta_group_id if owner else None,
                "cta_reference_sources": sources,
            }
            visual_choices.append(VisualChoice.model_validate(fields))
        source_blocks.append({
            "block_id": block_id, "beats": source_beats,
        })
        visual_blocks.append(VisualGroup(
            block_id=block_id, beats=visual_choices,
        ))
    boundary_issues = [*issues, *boundary_issues]
    boundaries = {
        "status": "blocked" if boundary_issues else "passed",
        "issues": boundary_issues,
        "blocks": source_blocks,
        "total_beats": sum(len(block["beats"]) for block in source_blocks),
    }
    result: dict[str, Any] = {
        "status": "blocked",
        "issues": list(boundary_issues),
        "boundaries": boundaries,
        "direction": direction.model_dump(mode="json"),
        "plan": None,
    }
    if issues:
        return result
    try:
        result["plan"] = assemble_plan(
            narrative_core=core, source_beats=source_blocks,
            visuals=WholeVideoVisuals(blocks=visual_blocks),
        )
    except (ValueError, TypeError) as exc:
        result["issues"].append({
            "rule_id": "VISUAL_ASSEMBLY", "severity": "error",
            "message": str(exc),
        })
        return result
    result["status"] = boundaries["status"]
    return result


def _suffix(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def apply_sparse_correction(
    original: SparseDirection, correction: SparseCorrection,
) -> SparseDirection:
    """Only user/model-authored revisions; never auto-split or rewrite beats."""
    known = {group.block_id: group for group in original.blocks}
    if len(known) != len(original.blocks):
        raise ValueError("Duplicate original block ID.")
    replaced = {group.block_id: group for group in correction.replaced_groups}
    if len(replaced) != len(correction.replaced_groups):
        raise ValueError("Duplicate replacement block ID.")
    if not set(replaced).issubset(known):
        raise ValueError("Replacement targets an unknown block.")
    beat_groups = {
        beat.beat_id: group.block_id
        for group in original.blocks for beat in group.beats
    }
    if len(beat_groups) != sum(len(group.beats) for group in original.blocks):
        raise ValueError("Duplicate original beat IDs.")
    patches = {patch.beat_id: patch for patch in correction.visual_patches}
    if len(patches) != len(correction.visual_patches):
        raise ValueError("Duplicate visual patch.")
    if not set(patches).issubset(beat_groups):
        raise ValueError("Patch targets unknown beat.")
    if any(beat_groups[ident] in replaced for ident in patches):
        raise ValueError("Patch targets a group already fully replaced.")
    result: list[SparseGroup] = []
    for group in original.blocks:
        if group.block_id in replaced:
            result.append(replaced[group.block_id])
            continue
        beats: list[SparseBeat] = []
        for beat in group.beats:
            patch = patches.get(beat.beat_id)
            if patch is None:
                beats.append(beat)
                continue
            values = {
                "beat_id": beat.beat_id,
                "text": beat.text,
                "beat_type": beat.beat_type,
                "visual_type": patch.visual_type,
                "editor_notes": beat.editor_notes if patch.editor_notes is None else patch.editor_notes,
            }
            if patch.visual_type in {"avatar_media", "media_video"}:
                values["prompt"] = patch.prompt
            if patch.visual_type == "media_video":
                values["video_prompt"] = patch.video_prompt
            if patch.visual_type == "media_image":
                values["image_prompts"] = patch.image_prompts
                if patch.generation_mode is not None:
                    values["generation_mode"] = patch.generation_mode
            beat_type = {
                "avatar": AvatarBeat, "avatar_media": AvatarMediaBeat,
                "media_image": ImageBeat, "media_video": VideoBeat,
            }[patch.visual_type]
            beats.append(beat_type.model_validate(values))
        result.append(group.model_copy(update={"beats": beats}))
    return SparseDirection(blocks=result)
