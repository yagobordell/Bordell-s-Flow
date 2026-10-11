"""Canonical global beat-cut contracts, using the proven B1.2 editorial method.

The model chooses ALL narrative cuts in one whole-script pass, using short
verbatim source-end anchors instead of error-prone model-calculated offsets.
Python derives literal beat text from resolved anchors and rejects every
overlong or orphan technical segment before any paid visual prompting.
No runtime code prescribes rhythm, beat counts or shot-change frequency.
"""
from __future__ import annotations

import copy
from typing import Any

from pydantic import Field

from ai_video_factory.bots.contracts import BeatType, StrictModel


class BeatCut(StrictModel):
    end_anchor: str = Field(
        min_length=1,
        description="Verbatim source phrase ending THIS beat, INCLUDING its punctuation",
    )
    occurrence: int = Field(ge=1, description="1-based occurrence after previous beat")
    beat_type: BeatType


class GroupCuts(StrictModel):
    block_id: int
    cuts: list[BeatCut] = Field(min_length=1)


class WholeScriptCuts(StrictModel):
    blocks: list[GroupCuts] = Field(min_length=1)


def _suffix(number: int) -> str:
    chars = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        chars = chr(65 + remainder) + chars
    return chars


def validate_and_materialize_cuts(
    groups: list[dict[str, Any]],
    proposal: WholeScriptCuts | dict[str, Any],
) -> dict[str, Any]:
    """Resolve exact source phrase anchors and apply both original B1.2 limits."""
    if isinstance(proposal, dict):
        proposal = WholeScriptCuts.model_validate(proposal)
    issues: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    if len(proposal.blocks) != len(groups):
        issues.append({
            "rule_id": "CUT_BLOCKS", "severity": "error",
            "message": "All B1.1 groups must be present in original order.",
        })
    for index, source in enumerate(groups):
        if index >= len(proposal.blocks):
            break
        candidate = proposal.blocks[index]
        block_id = source["block_id"]
        if candidate.block_id != block_id:
            issues.append({
                "rule_id": "CUT_BLOCK_ORDER", "severity": "error",
                "message": f"Expected block {block_id}, got {candidate.block_id}.",
            })
            continue
        text = source["text"]
        start = 0
        beats: list[dict[str, Any]] = []
        for count, cut in enumerate(candidate.cuts, 1):
            beat_id = f"{block_id}{_suffix(count)}"
            anchor = cut.end_anchor
            search_at = start
            match = -1
            for _ in range(cut.occurrence):
                match = text.find(anchor, search_at)
                if match < 0:
                    break
                search_at = match + len(anchor)
            if match < start:
                issues.append({
                    "rule_id": "CUT_ANCHOR", "severity": "error",
                    "beat_ids": [beat_id],
                    "message": (
                        f"Exact end anchor {anchor!r} occurrence {cut.occurrence} "
                        "is absent after the preceding beat. Copy real source words."
                    ),
                })
                continue
            end = match + len(anchor)
            # B1.2 ownership: inter-beat separators belong to preceding beat.
            while end < len(text) and text[end].isspace():
                end += 1
            segment = text[start:end]
            if not segment.strip():
                issues.append({
                    "rule_id": "CUT_EMPTY", "severity": "error",
                    "beat_ids": [beat_id],
                    "message": "Cut produced a whitespace-only beat.",
                })
            if len(segment.strip()) > 100 or len(segment.split()) > 40:
                issues.append({
                    "rule_id": "BEAT_LENGTH", "severity": "error",
                    "beat_ids": [beat_id],
                    "message": (
                        f"Beat has {len(segment.strip())} stripped characters "
                        f"and {len(segment.split())} words (max 100 chars, 40 words). "
                        "Choose better semantic cut positions; do not change source text."
                    ),
                    "source_start": start, "source_end": end,
                })
            beats.append({
                "beat_id": beat_id, "beat_type": cut.beat_type,
                "text": segment,
            })
            start = end
        if start != len(text):
            issues.append({
                "rule_id": "CUT_TILING", "severity": "error",
                "message": (
                    f"Block {block_id} ends at offset {start}, "
                    f"but its exact source length is {len(text)}."
                ),
            })
        blocks.append({
            "block_id": block_id,
            "beats": beats,
        })
    return {
        "status": "blocked" if issues else "passed",
        "issues": issues,
        "blocks": blocks,
        "total_beats": sum(len(b["beats"]) for b in blocks),
    }


def combine_cut_corrections(
    original: WholeScriptCuts,
    replacements: list[GroupCuts],
) -> WholeScriptCuts:
    """Replace ONLY selected whole group cut sets; never edit narration."""
    existing = {group.block_id: group for group in original.blocks}
    if len(existing) != len(original.blocks):
        raise ValueError("Duplicate original B1.1 group cut IDs.")
    if len({g.block_id for g in replacements}) != len(replacements):
        raise ValueError("Duplicate group IDs in cut corrections.")
    if not set(g.block_id for g in replacements).issubset(existing):
        raise ValueError("A correction referred to an unknown narrative group.")
    for group in replacements:
        existing[group.block_id] = group
    return WholeScriptCuts(blocks=[
        copy.deepcopy(existing[g.block_id]) for g in original.blocks
    ])
