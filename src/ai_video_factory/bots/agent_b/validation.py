"""Deterministic acceptance gate for Agent B's *single* professional visual plan.

Creative decisions are not graded here. Every diagnostic is in English and references
source-owned data; the planner never receives permission to rewrite source or gates.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from ai_video_factory.bots.contracts import MaterializedBlock, ProfessionalVisualBeat

_LABEL = re.compile(r"^([A-Z]+)([1-9][0-9]*)\.\s+(.+)$", re.DOTALL)
_OLD_NUMBER = re.compile(r"^[1-9][0-9]*\.\s+")
_ALLOWED_SPLIT = frozenset({"grid", "independent"})


def _beat_suffix(number: int) -> str:
    suffix = ""
    while number:
        number, rem = divmod(number - 1, 26)
        suffix = chr(ord("A") + rem) + suffix
    return suffix


@dataclass(frozen=True)
class PlanIssue:
    code: str
    message: str
    block_id: int | None = None
    beat_ids: tuple[str, ...] = ()
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sequence_index(plan: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Derive logical A1/A2 labels; never maintain a separate writable sequence ledger."""
    index: dict[str, list[dict[str, Any]]] = {}
    for block in plan.get("blocks", []):
        if not isinstance(block, dict):
            continue
        for beat in block.get("beats", []):
            if not isinstance(beat, dict):
                continue
            shots = beat.get("visual_subshots")
            assets = shots if isinstance(shots, list) and shots else [beat]
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                prompt = asset.get("prompt")
                match = _LABEL.match(prompt) if isinstance(prompt, str) else None
                if match:
                    name, ordinal, _ = match.groups()
                    index.setdefault(name, []).append({
                        "label": f"{name}{ordinal}",
                        "ordinal": int(ordinal),
                        "block_id": block.get("block_id"),
                        "beat_id": beat.get("beat_id"),
                        "asset_id": asset.get("asset_id", beat.get("beat_id")),
                        "visual_type": beat.get("visual_type"),
                        "cta_group_id": beat.get("cta_group_id"),
                    })
    return index


def validate_unified_plan(
    plan: Mapping[str, Any],
    source_blocks: Sequence[MaterializedBlock],
    narrative_core: Mapping[str, Any],
) -> list[PlanIssue]:
    """Return *all* detected deterministic failures; no model calls or paid work."""
    errors: list[PlanIssue] = []

    def issue(code: str, message: str, block_id: int | None = None,
              beat_ids: tuple[str, ...] = (), expected: Any = None,
              actual: Any = None) -> None:
        errors.append(PlanIssue(code, message, block_id, beat_ids, expected, actual))

    if not isinstance(plan, Mapping):
        return [PlanIssue("INVALID_PLAN", "The plan must be an object.")]
    if plan.get("schema_version") != "b-pipeline-v1":
        issue("SCHEMA_VERSION", "Unexpected audiovisual plan schema version.",
              expected="b-pipeline-v1", actual=plan.get("schema_version"))
    if plan.get("visual_style") != "profesional":
        issue("VISUAL_STYLE", "Agent B requires the professional visual style.",
              expected="profesional", actual=plan.get("visual_style"))
    if plan.get("narrative_core") != dict(narrative_core):
        issue("NARRATIVE_CORE", "The B1.1 narrative core must remain unchanged.")
    blocks = plan.get("blocks")
    if not isinstance(blocks, list):
        return errors + [PlanIssue("INVALID_BLOCKS", "The blocks field must be an array.")]
    expected_order = [block.block_id for block in source_blocks]
    actual_order = [block.get("block_id") if isinstance(block, dict) else None
                    for block in blocks]
    if expected_order != actual_order:
        issue("BLOCK_ORDER", "B1.1 block count and order must be unchanged.",
              expected=expected_order, actual=actual_order)
    sources = {block.block_id: block for block in source_blocks}
    spoken: list[tuple[int, str, int]] = []
    seen_ids: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict):
            issue("INVALID_BLOCK", "Every block must be an object.")
            continue
        block_id = block.get("block_id")
        source = sources.get(block_id) if isinstance(block_id, int) else None
        if source is None:
            issue("UNKNOWN_BLOCK", "Block is not present in the immutable B1.1 source.",
                  actual=block_id)
            continue
        for field in ("type", "emotional_entry", "emotional_exit"):
            if block.get(field) != getattr(source, field):
                issue("BLOCK_METADATA", f"B1.1 field {field} has changed.", block_id,
                      expected=getattr(source, field), actual=block.get(field))
        beats = block.get("beats")
        if not isinstance(beats, list) or not beats:
            issue("BEATS_MISSING", "A source block needs at least one beat.", block_id)
            continue
        texts: list[str] = []
        for number, beat in enumerate(beats, 1):
            if not isinstance(beat, dict):
                issue("INVALID_BEAT", "Every beat must be an object.", block_id)
                continue
            beat_id = beat.get("beat_id")
            ids = (beat_id,) if isinstance(beat_id, str) else ()
            required = f"{block_id}{_beat_suffix(number)}"
            if beat_id != required or beat_id in seen_ids:
                issue("BEAT_ID", "Beat IDs must be unique and sequential within each block.",
                      block_id, ids, required, beat_id)
            if isinstance(beat_id, str):
                seen_ids.add(beat_id)
            text = beat.get("text")
            if not isinstance(text, str) or not text.strip():
                issue("BEAT_TEXT", "Beat narration must be nonempty source text.", block_id, ids)
                continue
            texts.append(text)
            if len(text.strip()) > 100:
                issue("BEAT_CHARACTERS", "Beat exceeds the 100-character limit.",
                      block_id, ids, 100, len(text.strip()))
            if len(text.split()) > 40:
                issue("BEAT_WORDS", "Beat exceeds the 40-word limit.",
                      block_id, ids, 40, len(text.split()))
            try:
                ProfessionalVisualBeat.model_validate({
                    key: beat.get(key) for key in (
                        "beat_id", "text", "beat_type", "visual_type", "prompt",
                        "video_prompt", "reference", "audio",
                    )
                })
            except ValidationError as exc:
                issue("VISUAL_CONTRACT", "Invalid professional visual/audio fields: "
                      + "; ".join(str(e["msg"]) for e in exc.errors()), block_id, ids)
            if beat.get("audio") is True:
                spoken.append((block_id, str(beat_id), len(text.strip())))
            else:
                if spoken and sum(item[2] for item in spoken) > 120:
                    issue("AVATAR_RUN", "Consecutive speaking beats exceed 120 characters.",
                          spoken[0][0], tuple(item[1] for item in spoken), 120,
                          sum(item[2] for item in spoken))
                spoken = []
            shots = beat.get("visual_subshots")
            is_candidate = (beat.get("visual_type") == "media_image"
                            and not beat.get("cta_group_id") and len(text) >= 70)
            if is_candidate and (not isinstance(shots, list) or len(shots) != 2):
                issue("REQUIRED_SUBSHOTS", "A 70+ character non-CTA still beat requires "
                      "exactly two visual subshots.", block_id, ids, 2,
                      len(shots) if isinstance(shots, list) else 0)
            if shots is not None:
                if not is_candidate:
                    issue("UNEXPECTED_SUBSHOTS", "Subshots are not permitted for this beat.",
                          block_id, ids)
                if not isinstance(shots, list) or len(shots) != 2 or any(
                    not isinstance(shot, dict) for shot in shots
                ):
                    continue
                modes = set()
                bodies = []
                for i, shot in enumerate(shots, 1):
                    asset_id = f"{beat_id}__{i}"
                    for key, expected in (("asset_id", asset_id),
                                          ("subshot_index", i), ("subshot_count", 2)):
                        if shot.get(key) != expected:
                            issue("SUBSHOT_ID", f"Invalid {key} for subshot {i}.",
                                  block_id, ids, expected, shot.get(key))
                    if shot.get("generation") not in _ALLOWED_SPLIT:
                        issue("SPLIT_MODE", "Subshot generation must be grid or independent.",
                              block_id, ids)
                    modes.add(shot.get("generation"))
                    if not isinstance(shot.get("prompt"), str) or not shot["prompt"].strip():
                        issue("SUBSHOT_PROMPT", "Subshot prompts must be nonempty.",
                              block_id, ids)
                    bodies.append(shot.get("prompt"))
                if len(modes) != 1:
                    issue("SPLIT_MODE", "Both subshots must use the same generation mode.",
                          block_id, ids)
                if beat.get("prompt") != shots[0].get("prompt"):
                    issue("PARENT_PROMPT", "The beat prompt must equal its first subshot prompt.",
                          block_id, ids)
                if isinstance(bodies[0], str) and bodies[0] == bodies[1]:
                    issue("DUPLICATE_SUBSHOTS", "Two subshots must not be identical.",
                          block_id, ids)
                if "grid" in modes and any(
                    not _LABEL.match(p) for p in bodies if isinstance(p, str)
                ):
                    issue("UNLABELLED_GRID", "Global grid subshots require A1/A2 labels.",
                          block_id, ids)
            if shots is None and isinstance(beat.get("prompt"), str):
                if _OLD_NUMBER.match(beat["prompt"]):
                    issue("LEGACY_GRID_LABEL", "Use global A1/A2 labels, not local numeric grids.",
                          block_id, ids)
        if "".join(texts) != source.text:
            issue("SOURCE_TEXT", "Beats must reconstruct the B1.1 block exactly.",
                  block_id, expected=source.text, actual="".join(texts))
        if block.get("type") == "close":
            final = beats[-1]
            if isinstance(final, dict) and not (
                final.get("visual_type") == "avatar" and final.get("prompt") is None
                and final.get("video_prompt") is None
                and final.get("reference") is False and final.get("audio") is True
            ):
                issue("CLOSE_ANCHOR", "The final close beat must be the unchanged avatar.",
                      block_id, (str(final.get("beat_id")),))
    if spoken and sum(item[2] for item in spoken) > 120:
        issue("AVATAR_RUN", "Consecutive speaking beats exceed 120 characters.",
              spoken[0][0], tuple(item[1] for item in spoken), 120,
              sum(item[2] for item in spoken))
    for name, assets in sequence_index(plan).items():
        actual = [item["ordinal"] for item in assets]
        expected = list(range(1, len(assets) + 1))
        if actual != expected:
            issue("SEQUENCE_ORDINAL", f"Sequence {name} must have consecutive global ordinals.",
                  expected=expected, actual=actual)
        if len(assets) == 1:
            issue("ORPHAN_SEQUENCE", f"Sequence {name} contains only one labeled asset.")
        kinds = {item["visual_type"] == "avatar_media" for item in assets}
        if len(kinds) > 1:
            issue("SEQUENCE_PANEL_FORMAT", f"Sequence {name} mixes avatar-media and B-roll frames.")
        ctas = {item["cta_group_id"] for item in assets}
        if len(ctas) > 1:
            issue("SEQUENCE_CTA_BOUNDARY", f"Sequence {name} crosses CTA ownership boundaries.")
    return errors
