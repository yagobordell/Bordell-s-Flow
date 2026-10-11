"""Sparse, source-text-first OpenAI contract for ONE joint audiovisual plan.

Each beat carries only fields relevant to its visual carrier. Editor notes,
identity references and ebook CTA ownership are group-side annotations, never
a dozen null/empty fields on every beat. The existing B2 contracts remain
unchanged and are populated only after deterministic source verification.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field

from ai_video_factory.agents.professional.direction import (
    DirectedBeat as LegacyDirectedBeat,
    DirectedGroup as LegacyDirectedGroup,
    WholeVideoDirection as LegacyWholeVideoDirection,
    materialize_direction,
)
from ai_video_factory.bots.contracts import BeatType, StrictModel


class BeatBase(StrictModel):
    beat_id: str
    text: str = Field(min_length=1)
    beat_type: BeatType


class AvatarBeat(BeatBase):
    visual_type: Literal["avatar"]


class AvatarMediaBeat(BeatBase):
    visual_type: Literal["avatar_media"]
    prompt: str = Field(min_length=1)


class ImageBeat(BeatBase):
    visual_type: Literal["media_image"]
    image_prompts: list[str] = Field(min_length=1, max_length=4)
    generation_mode: Literal["grid", "independent"]


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
    # Lists are declared once per group, not on every beat.
    editor_notes: list[BeatNote]
    presenter_references: list[str]
    cta: list[CTAAssignment]


class SparseDirection(StrictModel):
    blocks: list[SparseGroup] = Field(min_length=1)


class AvatarPatch(StrictModel):
    beat_id: str
    visual_type: Literal["avatar"]


class AvatarMediaPatch(StrictModel):
    beat_id: str
    visual_type: Literal["avatar_media"]
    prompt: str = Field(min_length=1)


class ImagePatch(StrictModel):
    beat_id: str
    visual_type: Literal["media_image"]
    image_prompts: list[str] = Field(min_length=1, max_length=4)
    generation_mode: Literal["grid", "independent"]


class VideoPatch(StrictModel):
    beat_id: str
    visual_type: Literal["media_video"]
    prompt: str = Field(min_length=1)
    video_prompt: str = Field(min_length=1)


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
    """Deterministically normalize a sparse model response into existing B2."""
    issues: list[dict[str, Any]] = []
    if len(groups) != len(direction.blocks):
        issues.append({
            "rule_id": "CUT_BLOCKS", "severity": "error",
            "message": "Every source group must be present exactly once.",
        })
    legacy_groups: list[LegacyDirectedGroup] = []
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
        notes: dict[str, list[dict[str, Any]]] = {}
        for note in group.editor_notes:
            if note.beat_id not in id_set:
                issues.append({
                    "rule_id": "EDITOR_NOTE_BEAT", "severity": "error",
                    "block_id": block_id, "beat_ids": [note.beat_id],
                    "message": "Editor note references an unknown beat.",
                })
                continue
            notes.setdefault(note.beat_id, []).append({
                "kind": note.kind,
                "instruction": note.instruction,
                "trigger": (
                    note.trigger.model_dump(mode="json")
                    if note.trigger is not None else None
                ),
            })
        if issues:
            continue
        converted: list[LegacyDirectedBeat] = []
        for beat, original in zip(group.beats, original_texts, strict=True):
            owner = cta.get(beat.beat_id)
            kind = beat.visual_type
            sources = list(owner.sources) if owner else None
            is_reference = (
                beat.beat_id in group.presenter_references
                or bool(sources)
            )
            fields: dict[str, Any] = {
                "beat_id": beat.beat_id,
                "end_anchor": original.rstrip(),
                "occurrence": 1,
                "beat_type": beat.beat_type,
                "visual_type": kind,
                "prompt": getattr(beat, "prompt", None),
                "video_prompt": getattr(beat, "video_prompt", None),
                "reference": is_reference,
                "audio": kind in ("avatar", "avatar_media"),
                "image_prompts": getattr(beat, "image_prompts", None),
                "generation_mode": getattr(beat, "generation_mode", None),
                "editor_notes": notes.get(beat.beat_id, []),
                "cta_group_id": owner.cta_group_id if owner else None,
                "cta_reference_sources": sources,
            }
            converted.append(LegacyDirectedBeat.model_validate(fields))
        legacy_groups.append(LegacyDirectedGroup(
            block_id=block_id, beats=converted,
        ))
    if issues:
        return {
            "status": "blocked", "issues": issues, "plan": None,
            "boundaries": {
                "status": "blocked", "issues": issues, "blocks": [],
                "total_beats": 0,
            },
        }
    return materialize_direction(
        groups, LegacyWholeVideoDirection(blocks=legacy_groups), core,
    )


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
            }
            if patch.visual_type in {"avatar_media", "media_video"}:
                values["prompt"] = patch.prompt
            if patch.visual_type == "media_video":
                values["video_prompt"] = patch.video_prompt
            if patch.visual_type == "media_image":
                values["image_prompts"] = patch.image_prompts
                values["generation_mode"] = patch.generation_mode
            beat_type = {
                "avatar": AvatarBeat, "avatar_media": AvatarMediaBeat,
                "media_image": ImageBeat, "media_video": VideoBeat,
            }[patch.visual_type]
            beats.append(beat_type.model_validate(values))
        result.append(group.model_copy(update={"beats": beats}))
    return SparseDirection(blocks=result)
