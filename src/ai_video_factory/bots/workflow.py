"""B1.1 → whole-video B1.2 / professional B2, preserving per-block checkpoints.

This module preserves the new string beat IDs and four visual formats exactly.
Downstream GPU clients must adopt these contracts before an end-to-end video
runner can be enabled; no lossy integer-shot conversion is performed.
"""

import asyncio
import base64
import copy
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ai_video_factory.config import settings
from ai_video_factory.providers.base import StructuredTextProvider
from ai_video_factory.providers.grid_numbering import (
    MAX_GRID_SEQUENCE_PANELS,
    alphabetic_sequence_id,
    format_grid_marker,
    parse_grid_marker,
)
from ai_video_factory.providers.image_grids import (
    GridPlanError,
    plan_image_jobs,
)
from ai_video_factory.providers.openai_batch_images import described_beats

from .contracts import (
    B2Input,
    B2InputBlock,
    B2Output,
    B11Output,
    B12Output,
    B21Output,
    Beat,
    BlockMeta,
    MaterializedBlock,
    ProfessionalB2BatchInput,
    ProfessionalB2BatchOutput,
    ProfessionalB2Block,
    ProfessionalB2CorrectionOutput,
    ProfessionalB2Output,
    ProfessionalVisualBeat,
    WholeB12Block,
    WholeB12Input,
    WholeB12Output,
    WholeProfessionalB2Input,
    WholeProfessionalB2Output,
)
from .cta import CTAPlacement, validate_placement
from .professional_cta import ProfessionalCTAVisualBeat
from .professional_cta_workflow import (
    apply_cta_visuals,
    detect_professional_ctas,
    direct_professional_cta,
)
from .validation_retries import MAX_VALIDATION_RETRIES, is_schema_failure, validation_feedback

# Transport adaptation to Responses API Structured Outputs.
_STRUCTURED_TRANSPORT = (
    "\n\nAPI transport: this call uses Responses API Structured Outputs. Return the "
    "specified JSON object directly via the supplied output schema, without an "
    "outer Markdown code fence. All schema, source-integrity and error rules apply."
)


class BPipelineValidationError(ValueError):
    """A model response violated its source-preserving stage contract."""


def _instructions(name: str) -> str:
    data = files("ai_video_factory.bots.prompts").joinpath(name).read_bytes()
    return data.decode("utf-8") + _STRUCTURED_TRANSPORT


def _payload(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _require_success(stage: str, error: object | None) -> None:
    if error is not None:
        raise BPipelineValidationError(f"{stage} returned fatal error: {error}")


def _beat_suffix(number: int) -> str:
    suffix = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        suffix = chr(ord("A") + remainder) + suffix
    return suffix


def _without_whitespace_and_punctuation(text: str) -> str:
    """Ignore layout, punctuation, and recoverable case while preserving content."""
    return "".join(
        character.lower() if len(character.lower()) == 1 else character
        for character in text
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def _restore_source_beat_texts(source: str, beats: list[Beat]) -> list[Beat]:
    """Rebuild exact beat slices from source and model-selected boundaries.

    The model chooses narrative boundaries and preserves the non-punctuation
    content stream. The application owns exact punctuation and whitespace.
    Separator characters before the next substantive source character stay
    with the preceding beat.
    """
    source_offsets = [
        index for index, character in enumerate(source)
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    ]
    source_cursor = 0
    source_start = 0
    restored: list[Beat] = []
    for index, beat in enumerate(beats):
        source_cursor += len(_without_whitespace_and_punctuation(beat.text))
        if source_cursor > len(source_offsets):
            raise BPipelineValidationError("B1.2 beat content exceeds its source block")
        if index + 1 == len(beats):
            source_end = len(source)
        elif source_cursor < len(source_offsets):
            source_end = source_offsets[source_cursor]
        else:
            source_end = len(source)
        if source_end < source_start:
            raise BPipelineValidationError("B1.2 beat boundaries are not in source order")
        restored.append(beat.model_copy(update={"text": source[source_start:source_end]}))
        source_start = source_end
    if source_cursor != len(source_offsets):
        raise BPipelineValidationError("B1.2 beats do not consume the complete source block")
    return restored


def _occurrences(text: str, fragment: str) -> list[int]:
    positions: list[int] = []
    offset = 0
    while True:
        position = text.find(fragment, offset)
        if position < 0:
            return positions
        positions.append(position)
        offset = position + 1


def _normalized_whitespace_with_offsets(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    source_offsets: list[int] = []
    for match in re.finditer(r"\s+|\S+", text):
        fragment = match.group()
        if fragment.isspace():
            normalized.append(" ")
            source_offsets.append(match.start())
        else:
            normalized.append(fragment)
            source_offsets.extend(range(match.start(), match.end()))
    return "".join(normalized), source_offsets


def _anchor_occurrences(
    normalized_script: str, source_offsets: list[int], anchor: str
) -> list[tuple[int, int]]:
    normalized_anchor = " ".join(anchor.split())
    return [
        (
            source_offsets[position],
            source_offsets[position + len(normalized_anchor) - 1] + 1,
        )
        for position in _occurrences(normalized_script, normalized_anchor)
    ]


def materialize_blocks(script: str, blocks: list[BlockMeta]) -> list[MaterializedBlock]:
    """Resolve whitespace-tolerant anchors into one exact ordered source tiling."""
    if not isinstance(script, str) or not script.strip():
        raise BPipelineValidationError("A nonempty authoritative raw script is required")
    if not blocks or [block.block_id for block in blocks] != list(range(1, len(blocks) + 1)):
        raise BPipelineValidationError("B1.1 block IDs must be consecutive from 1")

    normalized_script, source_offsets = _normalized_whitespace_with_offsets(script)
    start_candidates: list[list[int]] = []
    last_candidates: list[list[tuple[int, int]]] = []
    for block in blocks:
        # Model anchors occasionally include boundary whitespace. Strip only
        # these lookup strings; block text still comes byte-for-byte from script.
        first = block.span.first_words.strip()
        last = block.span.last_words.strip()
        if not first or not last:
            raise BPipelineValidationError("B1.1 anchors must contain substantive text")
        first_positions = [
            start for start, _ in _anchor_occurrences(normalized_script, source_offsets, first)
        ]
        last_positions = _anchor_occurrences(normalized_script, source_offsets, last)
        if not first_positions or not last_positions:
            raise BPipelineValidationError(
                f"B1.1 block {block.block_id} anchor was not found verbatim in raw script"
            )
        start_candidates.append(first_positions)
        last_candidates.append(last_positions)

    def has_valid_end(block_index: int, first_position: int, end: int) -> bool:
        return any(
            last_start >= first_position
            and last_end <= end
            and not script[last_end:end].strip()
            for last_start, last_end in last_candidates[block_index]
        )

    solutions: list[tuple[int, ...]] = []
    selected_starts: list[int] = []

    def search_tilings(block_index: int) -> None:
        if len(solutions) > 1:
            return
        if block_index == len(blocks):
            if has_valid_end(len(blocks) - 1, selected_starts[-1], len(script)):
                solutions.append(tuple(selected_starts))
            return

        for position in start_candidates[block_index]:
            if selected_starts and position <= selected_starts[-1]:
                continue
            if not selected_starts:
                if script[:position].strip():
                    continue
            elif not has_valid_end(block_index - 1, selected_starts[-1], position):
                continue
            selected_starts.append(position)
            search_tilings(block_index + 1)
            selected_starts.pop()
            if len(solutions) > 1:
                return

    search_tilings(0)
    if not solutions:
        raise BPipelineValidationError(
            "B1.1 anchors cannot tile the raw script in block order with exact boundaries"
        )
    if len(solutions) > 1:
        raise BPipelineValidationError(
            "B1.1 anchors allow multiple valid block tilings; boundaries are ambiguous"
        )
    starts = solutions[0]

    result: list[MaterializedBlock] = []
    for index, block in enumerate(blocks):
        start = 0 if index == 0 else starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(script)
        result.append(
            MaterializedBlock(
                block_id=block.block_id,
                type=block.type,
                emotional_entry=block.emotional_entry,
                emotional_exit=block.emotional_exit,
                text=script[start:end],
            )
        )
    if "".join(block.text for block in result) != script:
        raise BPipelineValidationError("Materialized blocks do not reconstruct the raw script")
    return result


def validate_beats(result: B12Output, block: MaterializedBlock) -> list[Beat]:
    _require_success("B1.2", result.error)
    if result.block_id != block.block_id or not result.beats:
        raise BPipelineValidationError("B1.2 returned a different block ID or no beats")
    for number, beat in enumerate(result.beats, start=1):
        if beat.beat_id != f"{block.block_id}{_beat_suffix(number)}":
            raise BPipelineValidationError(f"Invalid B1.2 beat ID {beat.beat_id}")
        if not _without_whitespace_and_punctuation(beat.text):
            raise BPipelineValidationError(f"Invalid B1.2 beat text: {beat.beat_id}")
    actual_text = "".join(beat.text for beat in result.beats)
    actual_words = _without_whitespace_and_punctuation(actual_text)
    expected_words = _without_whitespace_and_punctuation(block.text)
    if actual_words != expected_words:
        mismatch = next(
            (
                index
                for index, (got, wanted) in enumerate(
                    zip(actual_words, expected_words, strict=False)
                )
                if got != wanted
            ),
            min(len(actual_words), len(expected_words)),
        )
        raise BPipelineValidationError(
            "B1.2 beat text differs from the materialized block beyond whitespace "
            f"or punctuation at normalized character {mismatch} in block {block.block_id}; "
            f"expected {expected_words[mismatch:mismatch + 25]!r}, "
            f"received {actual_words[mismatch:mismatch + 25]!r}"
        )
    restored_beats = _restore_source_beat_texts(block.text, result.beats)
    word_errors = [
        f"B1.2 beat {beat.beat_id} has {len(beat.text.split())} words (maximum 40)"
        for beat in restored_beats if len(beat.text.split()) > 40
    ]
    if word_errors:
        raise BPipelineValidationError("; ".join(word_errors))
    length_errors = _beat_character_length_errors(restored_beats)
    if length_errors:
        raise BPipelineValidationError("; ".join(length_errors))
    result.beats = restored_beats
    return restored_beats


def _beat_character_length_errors(beats: list[Beat]) -> list[str]:
    """Enforce the decoded narration character maximum, excluding boundary whitespace."""
    errors: list[str] = []
    for beat in beats:
        characters = len(beat.text.strip())
        if characters > 100:
            errors.append(
                f"B1.2 beat {beat.beat_id} has {characters} characters (maximum 100); "
                "split it into two or more beats"
            )
    return errors


_MAX_CONSECUTIVE_AVATAR_CHARACTERS = 120
_PRESENTER_VISUAL_TYPES = frozenset({"avatar", "avatar_media"})
_PROFESSIONAL_B2_BATCH_SIZE = 4


def _overlong_audio_runs_in_plan(
    plan: dict[str, Any],
) -> list[list[tuple[str, int]]]:
    """Return every consecutive audio=true run over the cap in a merged plan."""
    overflows: list[list[tuple[str, int]]] = []
    run: list[tuple[str, int]] = []
    run_chars = 0
    for block in plan.get("blocks", []):
        for beat in block.get("beats", []):
            if beat.get("audio"):
                item = (str(beat.get("beat_id", "?")), len(str(beat.get("text", "")).strip()))
                run.append(item)
                run_chars += item[1]
            else:
                if run_chars > _MAX_CONSECUTIVE_AVATAR_CHARACTERS:
                    overflows.append(run)
                run = []
                run_chars = 0
    if run_chars > _MAX_CONSECUTIVE_AVATAR_CHARACTERS:
        overflows.append(run)
    return overflows


@dataclass(frozen=True)
class _AvatarRunBeat:
    block_id: int
    beat_id: str
    characters: int


def _overlong_avatar_run(
    outputs: list[B2Output | ProfessionalB2Output] | tuple[B2Output | ProfessionalB2Output, ...],
) -> tuple[list[_AvatarRunBeat], int] | None:
    """Find the first overlong spoken run in whole-video beat order."""
    return next(iter(_overlong_avatar_runs(outputs)), None)


def _overlong_avatar_runs(
    outputs: list[B2Output | ProfessionalB2Output] | tuple[B2Output | ProfessionalB2Output, ...],
) -> list[tuple[list[_AvatarRunBeat], int]]:
    """Find every overlong spoken run in whole-video beat order.

    Professional B2 defines a run by consecutive audio=true beats, regardless of
    visual_type. Casero has no audio flag, so it retains the historical
    avatar/avatar_media presenter-run rule.
    """
    run: list[_AvatarRunBeat] = []
    characters = 0
    overflows: list[tuple[list[_AvatarRunBeat], int]] = []
    for output in outputs:
        if not output.blocks:
            raise BPipelineValidationError("B2 output is missing its block")
        block = output.blocks[0]
        professional = isinstance(output, ProfessionalB2Output)
        for beat in block.beats:
            continues_run = (
                beat.audio
                if professional
                else beat.visual_type in _PRESENTER_VISUAL_TYPES
            )
            if not continues_run:
                if characters > _MAX_CONSECUTIVE_AVATAR_CHARACTERS:
                    overflows.append((run, characters))
                run = []
                characters = 0
                continue
            length = len(beat.text.strip())
            run.append(
                _AvatarRunBeat(
                    block_id=block.block_id,
                    beat_id=beat.beat_id,
                    characters=length,
                )
            )
            characters += length
    if characters > _MAX_CONSECUTIVE_AVATAR_CHARACTERS:
        overflows.append((run, characters))
    return overflows


def _avatar_run_error(
    run: list[_AvatarRunBeat], characters: int, *, professional: bool = False,
) -> str:
    beats = ", ".join(f"{beat.beat_id} ({beat.characters})" for beat in run)
    if professional:
        return (
            f"B2 consecutive audio=true beats {beats} total {characters} characters "
            f"(maximum {_MAX_CONSECUTIVE_AVATAR_CHARACTERS}); assign a faithful "
            "audio=false visual to a discretionary beat without changing frozen "
            "narration or mandatory anchors"
        )
    return (
        f"B2 consecutive avatar/avatar_media beats {beats} total {characters} characters "
        f"(maximum {_MAX_CONSECUTIVE_AVATAR_CHARACTERS}); assign a faithful non-presenter "
        "visual to a discretionary beat without changing frozen narration or mandatory anchors"
    )


@dataclass(frozen=True)
class _AvatarRunBreak:
    beat_id: str
    left_characters: int
    right_characters: int

    @property
    def sufficient(self) -> bool:
        return max(self.left_characters, self.right_characters) <= (
            _MAX_CONSECUTIVE_AVATAR_CHARACTERS
        )


def _preferred_avatar_run_break(
    outputs: tuple[B2Output | ProfessionalB2Output, ...],
    run: list[_AvatarRunBeat],
    characters: int,
    *,
    professional: bool,
) -> _AvatarRunBreak | None:
    """Choose the most balanced discretionary beat for breaking one overlong run."""
    blocks = {
        output.blocks[0].block_id: output.blocks[0]
        for output in outputs
        if output.blocks
    }
    candidates: list[tuple[int, int, str, int, int]] = []
    offset = 0
    for beat in run:
        left = offset
        right = characters - left - beat.characters
        offset += beat.characters
        block = blocks.get(beat.block_id)
        if block is None:
            continue
        index = next(
            (
                item_index
                for item_index, item in enumerate(block.beats)
                if item.beat_id == beat.beat_id
            ),
            None,
        )
        if index is None:
            continue
        mandatory = (
            professional
            and block.type == "close"
            and index == len(block.beats) - 1
        )
        if mandatory:
            continue
        candidates.append(
            (
                max(left, right),
                -beat.characters,
                beat.beat_id,
                left,
                right,
            )
        )
    if not candidates:
        return None
    _, _, beat_id, left, right = min(candidates)
    return _AvatarRunBreak(
        beat_id=beat_id,
        left_characters=left,
        right_characters=right,
    )


def repair_frozen_b2_text(
    result: B2Output | ProfessionalB2Output,
    supplied: B2Input,
) -> B2Output | ProfessionalB2Output:
    """Restore B1.2 narration before visual validation, without a retry.

    B2 owns visual decisions only. When the frozen B1.2 beat IDs and order
    match, their text and type are restored locally while visual fields remain
    unchanged. Missing, duplicate, reordered, or added IDs and changed beat
    counts remain errors because their visual assignments cannot be mapped
    safely to source beats.
    """
    if result.error is not None or not result.blocks:
        return result
    actual = result.blocks[0]
    expected = supplied.blocks[0]
    expected_ids = [beat.beat_id for beat in expected.beats]
    actual_ids = [beat.beat_id for beat in actual.beats]
    if len(actual.beats) != len(expected.beats) or actual_ids != expected_ids:
        return result
    repaired = result.model_copy(deep=True)
    for visual, upstream in zip(repaired.blocks[0].beats, expected.beats, strict=True):
        visual.beat_id = upstream.beat_id
        visual.beat_type = upstream.beat_type
        visual.text = upstream.text
    return repaired


def validate_visuals(
    result: B2Output | ProfessionalB2Output,
    supplied: B2Input,
    *,
    check_audio_run: bool = True,
) -> None:
    _require_success("B2", result.error)
    if result.narrative_core != supplied.narrative_core or not result.blocks:
        raise BPipelineValidationError("B2 altered the narrative core or omitted the block")
    actual = result.blocks[0]
    expected = supplied.blocks[0]
    actual_meta = actual.model_dump(exclude={"beats"})
    expected_meta = expected.model_dump(exclude={"beats"})
    if actual_meta != expected_meta:
        differences = [
            f"{name}: expected {expected_meta.get(name)!r}, got {actual_meta.get(name)!r}"
            for name in expected_meta.keys() | actual_meta.keys()
            if expected_meta.get(name) != actual_meta.get(name)
        ]
        raise BPipelineValidationError(
            f"B2 altered frozen B1.1 block metadata for block {expected.block_id}: "
            + "; ".join(sorted(differences))
        )
    if len(actual.beats) != len(expected.beats):
        raise BPipelineValidationError("B2 changed the number of beats")
    for visual, upstream in zip(actual.beats, expected.beats, strict=True):
        restored = visual.model_dump(exclude={
            "visual_type", "description", "prompt", "video_prompt", "reference", "audio"
        })
        expected_beat = upstream.model_dump()
        if restored != expected_beat:
            differences = [
                f"{name}: expected {expected_beat.get(name)!r}, "
                f"got {restored.get(name)!r}"
                for name in expected_beat.keys() | restored.keys()
                if expected_beat.get(name) != restored.get(name)
            ]
            raise BPipelineValidationError(
                f"B2 changed a frozen B1.2 beat {upstream.beat_id}: "
                + "; ".join(sorted(differences))
            )
    if check_audio_run:
        overflow = _overlong_avatar_run((result,))
        if overflow is not None:
            raise BPipelineValidationError(
                _avatar_run_error(
                    *overflow, professional=isinstance(result, ProfessionalB2Output)
                )
            )


_B21_IMAGE_SPLIT_THRESHOLD = 70
def _b21_prompt_body(prompt: str) -> str:
    marker = parse_grid_marker(prompt)
    return marker.body.strip() if marker is not None else prompt.strip()


def _b21_numbered_groups(
    block: dict[str, object],
) -> list[tuple[str | None, list[str]]]:
    """Return legacy runs and lettered groups, including nonconsecutive beats."""
    lettered: dict[str, list[tuple[int, int, str]]] = {}
    legacy: list[tuple[int, list[str]]] = []
    current: list[str] = []
    current_start = 0
    for position, beat in enumerate(block["beats"]):
        marker = parse_grid_marker(beat.get("prompt"))
        if marker is not None and marker.style == "lettered":
            if current:
                legacy.append((current_start, current))
                current = []
            if marker.sequence_id is not None:
                lettered.setdefault(marker.sequence_id, []).append(
                    (position, marker.panel_number, beat["beat_id"])
                )
            continue
        if marker is None or marker.style != "legacy":
            if current:
                legacy.append((current_start, current))
                current = []
            continue
        if marker.panel_number == 1:
            if current:
                legacy.append((current_start, current))
            current_start = position
            current = [beat["beat_id"]]
        elif current and marker.panel_number == len(current) + 1:
            current.append(beat["beat_id"])
        else:
            if current:
                legacy.append((current_start, current))
            current_start = position
            current = [beat["beat_id"]]
    if current:
        legacy.append((current_start, current))

    groups: list[tuple[int, str | None, list[str]]] = [
        (start, None, beat_ids) for start, beat_ids in legacy if len(beat_ids) >= 2
    ]
    for label, members in lettered.items():
        members.sort(key=lambda item: item[0])
        beat_ids = [
            beat_id
            for _position, _number, beat_id in sorted(members, key=lambda item: item[1])
        ]
        groups.append((members[0][0], label, beat_ids))
    groups.sort(key=lambda item: item[0])
    return [(label, beat_ids) for _start, label, beat_ids in groups]


def _b21_split_candidates(plan: dict[str, object]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for block in plan["blocks"]:
        for beat in block["beats"]:
            if beat.get("visual_type") != "media_image" or beat.get("cta_group_id"):
                continue
            characters = len(str(beat.get("text", "")))
            if characters >= _B21_IMAGE_SPLIT_THRESHOLD:
                candidates.append({
                    "block_id": block["block_id"],
                    "beat_id": beat["beat_id"],
                    "character_count": characters,
                })
    return candidates


def _b21_numbered_runs(block: dict[str, object]) -> list[list[str]]:
    """Return all B2 grid groups without requiring adjacency in narration."""
    return [beat_ids for _label, beat_ids in _b21_numbered_groups(block)]


def validate_b21(
    result: B21Output,
    *,
    source_plan: dict[str, object],
    split_candidates: list[dict[str, object]],
) -> None:
    """B2.1 may change visual prompts only; all B2 structure stays frozen."""
    _require_success("B2.1", result.error)
    if result.modified_beats is None:
        raise BPipelineValidationError("B2.1 returned no modified_beats list")
    source = {
        (block["block_id"], beat["beat_id"]): beat
        for block in source_plan["blocks"] for beat in block["beats"]
    }
    candidate_keys = {
        (item["block_id"], item["beat_id"]) for item in split_candidates
    }
    numbered_keys = {
        (block["block_id"], beat_id)
        for block in source_plan["blocks"]
        for run in _b21_numbered_runs(block)
        for beat_id in run
    }
    seen: set[tuple[int, str]] = set()
    split_seen: set[tuple[int, str]] = set()
    for patch in result.modified_beats:
        key = patch.block_id, patch.beat_id
        if key in seen:
            raise BPipelineValidationError(
                f"B2.1 emitted duplicate patch for {patch.beat_id}"
            )
        seen.add(key)
        original = source.get(key)
        if original is None:
            raise BPipelineValidationError(
                f"B2.1 emitted unknown beat {patch.beat_id}"
            )
        if original.get("prompt") is None:
            raise BPipelineValidationError(
                f"B2.1 cannot edit reused base avatar {patch.beat_id}"
            )
        visual_type = original.get("visual_type")
        if visual_type == "media_video":
            if (
                not isinstance(patch.video_prompt, str)
                or not patch.video_prompt.strip()
                or patch.second_prompt is not None
                or patch.split_mode is not None
            ):
                raise BPipelineValidationError(
                    f"B2.1 media_video patch {patch.beat_id} has invalid visual fields"
                )
        else:
            if patch.video_prompt is not None:
                raise BPipelineValidationError(
                    f"B2.1 {patch.beat_id} cannot add a video_prompt"
                )
            should_split = key in candidate_keys
            if should_split:
                if (
                    visual_type != "media_image"
                    or patch.second_prompt is None
                    or patch.split_mode is None
                ):
                    raise BPipelineValidationError(
                        f"B2.1 split candidate {patch.beat_id} requires two image prompts"
                    )
                if key in numbered_keys and patch.split_mode != "grid":
                    raise BPipelineValidationError(
                        f"B2.1 numbered image {patch.beat_id} must stay in its grid"
                    )
                split_seen.add(key)
            elif patch.second_prompt is not None or patch.split_mode is not None:
                raise BPipelineValidationError(
                    f"B2.1 may split only deterministic media_image candidates: "
                    f"{patch.beat_id}"
                )
    missing = candidate_keys - split_seen
    if missing:
        names = ", ".join(beat_id for _block, beat_id in sorted(missing))
        raise BPipelineValidationError(
            "B2.1 omitted required long-image split candidates: " + names
        )


def _b21_pack_grid_units(
    units: list[tuple[dict[str, object], list[str]]],
) -> list[list[tuple[dict[str, object], list[str]]]]:
    """Pack logical continuity runs of up to 20 assets, preserving beat subshot pairs."""
    chunks: list[list[tuple[dict[str, object], list[str]]]] = []
    current: list[tuple[dict[str, object], list[str]]] = []
    count = 0
    for unit in units:
        size = len(unit[1])
        if current and count + size > MAX_GRID_SEQUENCE_PANELS:
            chunks.append(current)
            current, count = [], 0
        current.append(unit)
        count += size
    if current:
        chunks.append(current)

    def panels(chunk: list[tuple[dict[str, object], list[str]]]) -> int:
        return sum(len(prompts) for _beat, prompts in chunk)

    if len(chunks) > 1 and panels(chunks[-1]) == 1:
        previous = chunks[-2]
        if len(previous) < 2:
            raise BPipelineValidationError(
                "B2.1 cannot reflow a numbered grid without orphaning a panel"
            )
        moved = previous.pop()
        chunks[-1].insert(0, moved)
        if panels(previous) < 2:
            raise BPipelineValidationError(
                "B2.1 grid reflow would create an orphan numbered panel"
            )
    return chunks


def apply_b21_review(
    source_plan: dict[str, object],
    result: B21Output,
) -> dict[str, object]:
    """Apply the validated visual-only patch and deterministically reflow grids."""
    reviewed = copy.deepcopy(source_plan)
    patches = {
        (patch.block_id, patch.beat_id): patch
        for patch in result.modified_beats or []
    }
    source_groups = {
        block["block_id"]: [
            (label, run) for label, run in _b21_numbered_groups(block)
            if not any(
                beat.get("cta_group_id") is not None
                for beat in block["beats"]
                if beat["beat_id"] in run
            )
        ]
        for block in source_plan["blocks"]
    }
    source_run_keys = {
        (block_id, beat_id)
        for block_id, groups in source_groups.items()
        for _label, run in groups for beat_id in run
    }
    used_grid_labels: dict[int, set[str]] = {}
    for block in source_plan["blocks"]:
        used_grid_labels[block["block_id"]] = {
            marker.sequence_id
            for beat in block["beats"]
            if (marker := parse_grid_marker(beat.get("prompt"))) is not None
            and marker.style == "lettered" and marker.sequence_id is not None
        }

    def allocate_grid_label(block_id: int) -> str:
        used = used_grid_labels.setdefault(block_id, set())
        index = 1
        while alphabetic_sequence_id(index) in used:
            index += 1
        label = alphabetic_sequence_id(index)
        used.add(label)
        return label

    for block in reviewed["blocks"]:
        for beat in block["beats"]:
            key = block["block_id"], beat["beat_id"]
            patch = patches.get(key)
            if patch is None or beat.get("cta_group_id") is not None:
                continue
            beat["prompt"] = patch.prompt.strip()
            if beat["visual_type"] == "media_video":
                beat["video_prompt"] = patch.video_prompt
            if patch.second_prompt is not None:
                generation = patch.split_mode
                first = _b21_prompt_body(patch.prompt)
                second = _b21_prompt_body(patch.second_prompt)
                if generation == "grid" and key not in source_run_keys:
                    label = allocate_grid_label(block["block_id"])
                    first = format_grid_marker("lettered", label, 1, first)
                    second = format_grid_marker("lettered", label, 2, second)
                beat["visual_subshots"] = [
                    {
                        "asset_id": f"{beat['beat_id']}__1",
                        "subshot_index": 1,
                        "subshot_count": 2,
                        "prompt": first,
                        "generation": generation,
                    },
                    {
                        "asset_id": f"{beat['beat_id']}__2",
                        "subshot_index": 2,
                        "subshot_count": 2,
                        "prompt": second,
                        "generation": generation,
                    },
                ]
                beat["prompt"] = first

    # Existing grid groups are authoritative. Insert B2.1 subshots in their
    # source group's logical order, then deterministically reflow each group.
    reviewed_by_block = {
        block["block_id"]: block for block in reviewed["blocks"]
    }
    for block_id, groups in source_groups.items():
        block = reviewed_by_block[block_id]
        beats = {beat["beat_id"]: beat for beat in block["beats"]}
        prepared: list[tuple[str | None, list[tuple[dict[str, object], list[str]]]]] = []
        for label, run in groups:
            units: list[tuple[dict[str, object], list[str]]] = []
            for beat_id in run:
                beat = beats[beat_id]
                subshots = beat.get("visual_subshots")
                if isinstance(subshots, list):
                    prompts = [_b21_prompt_body(str(item["prompt"])) for item in subshots]
                else:
                    prompts = [_b21_prompt_body(str(beat["prompt"]))]
                units.append((beat, prompts))
            prepared.extend(
                (label, chunk) for chunk in _b21_pack_grid_units(units)
            )

        reserved_labels = {
            marker.sequence_id
            for beat in block["beats"]
            if (marker := parse_grid_marker(beat.get("prompt"))) is not None
            and marker.style == "lettered" and marker.sequence_id is not None
        }
        reserved_labels.update(label for label, _chunk in prepared if label is not None)
        used_labels: set[str] = set()
        for label, chunk in prepared:
            if label is None:
                next_label = None
            elif label not in used_labels:
                next_label = label
                used_labels.add(label)
            else:
                index = 1
                while alphabetic_sequence_id(index) in reserved_labels | used_labels:
                    index += 1
                next_label = alphabetic_sequence_id(index)
                used_labels.add(next_label)
            number = 1
            for beat, prompts in chunk:
                subshots = beat.get("visual_subshots")
                if isinstance(subshots, list):
                    for item, body in zip(subshots, prompts, strict=True):
                        item["generation"] = "grid"
                        item["prompt"] = format_grid_marker(
                            "lettered" if next_label is not None else "legacy",
                            next_label, number, body,
                        )
                        number += 1
                    beat["prompt"] = subshots[0]["prompt"]
                else:
                    beat["prompt"] = format_grid_marker(
                        "lettered" if next_label is not None else "legacy",
                        next_label, number, prompts[0],
                    )
                    number += 1
    return reviewed


def _professional_plan(
    outputs: list[ProfessionalB2Output] | tuple[ProfessionalB2Output, ...],
    *,
    cta_groups: tuple[tuple[str, ...], ...] = (),
    cta_overrides: dict[str, ProfessionalCTAVisualBeat] | None = None,
) -> dict[str, object]:
    blocks = [item.blocks[0].model_dump() for item in outputs if item.blocks]
    if cta_groups:
        blocks = apply_cta_visuals(blocks, cta_groups, cta_overrides or {})
    return {"visual_style": "profesional", "blocks": blocks}


def _reviewed_professional_plan(
    outputs: list[ProfessionalB2Output] | tuple[ProfessionalB2Output, ...],
    b21: B21Output | None,
    *,
    cta_groups: tuple[tuple[str, ...], ...] = (),
    cta_overrides: dict[str, ProfessionalCTAVisualBeat] | None = None,
) -> dict[str, object]:
    plan = _professional_plan(
        outputs, cta_groups=cta_groups, cta_overrides=cta_overrides,
    )
    return apply_b21_review(plan, b21) if b21 is not None else plan


def _grid_error_block_id(error: BaseException) -> int | None:
    """Map a grid marker error to its source B1.1 block when possible."""
    message = str(error)
    beat_match = re.search(r"(?:at |description at )(\d+)[A-Z]+", message)
    if beat_match is not None:
        return int(beat_match.group(1))
    block_match = re.search(r"\bin block (\d+)\b", message, re.IGNORECASE)
    if block_match is not None:
        return int(block_match.group(1))
    return None


def _renumber_lettered_panels(outputs: list[ProfessionalB2Output]) -> None:
    """Number each lettered grid's panels 1..N in narrative order within its block.

    The application owns grid numbering, so repeated or skipped panel numbers from
    the model are corrected here instead of failing the whole batch.
    """
    for output in outputs:
        for block in output.blocks:
            counts: dict[str, int] = {}
            for beat in block.beats:
                marker = parse_grid_marker(beat.prompt)
                if marker is None or marker.style != "lettered" or marker.sequence_id is None:
                    continue
                counts[marker.sequence_id] = counts.get(marker.sequence_id, 0) + 1
                beat.prompt = format_grid_marker(
                    "lettered", marker.sequence_id, counts[marker.sequence_id], marker.body,
                )


def _professional_grid_preflight(
    plan: dict[str, object] | list[ProfessionalB2Output] | tuple[ProfessionalB2Output, ...],
) -> None:
    """Verify the final numbered grid plan before model review or paid images."""
    if not isinstance(plan, dict):
        plan = _professional_plan(plan)
    plan_image_jobs(plan, described_beats(plan))


def _professional_audio_groups(
    plan: dict[str, object] | list[ProfessionalB2Output] | tuple[ProfessionalB2Output, ...],
) -> list[list[tuple[int, dict[str, object]]]]:
    """Return maximal whole-video runs of consecutive reviewed audio=true beats."""
    if not isinstance(plan, dict):
        plan = _professional_plan(plan)
    groups: list[list[tuple[int, dict[str, object]]]] = []
    current: list[tuple[int, dict[str, object]]] = []
    for block in plan["blocks"]:
        for beat in block["beats"]:
            if (
                current and beat.get("cta_group_id")
                != current[-1][1].get("cta_group_id")
            ):
                groups.append(current)
                current = []
            if beat.get("audio") is True:
                current.append((block["block_id"], beat))
            elif current:
                groups.append(current)
                current = []
    if current:
        groups.append(current)
    return groups


@dataclass(frozen=True)
class BPipelineResult:
    b11: B11Output
    b12: tuple[B12Output, ...]
    b2: tuple[B2Output | ProfessionalB2Output, ...]
    b21: B21Output | None = None
    cta_groups: tuple[tuple[str, ...], ...] = ()
    cta_overrides: dict[str, ProfessionalCTAVisualBeat] = field(default_factory=dict)
    visual_style: Literal["casero", "profesional"] = "casero"
    b2_correction_counts: dict[int, int] = field(default_factory=dict)
    b2_response_ids: dict[int, str] = field(default_factory=dict)

    def merged_b12_output(self) -> dict[str, object]:
        """Application-owned complete B1.2 join, ordered by frozen block ID."""
        if self.b11.narrative_core is None:
            raise BPipelineValidationError("B1.1 narrative core is missing")
        return {
            "schema_version": "b-pipeline-merged-v1",
            "pipeline_stage": "B1.2",
            "narrative_core": self.b11.narrative_core.model_dump(),
            "blocks": [
                {
                    "block_id": item.block_id,
                    "beats": [beat.model_dump() for beat in item.beats or []],
                }
                for item in self.b12
            ],
        }

    def merged_b2_output(self) -> dict[str, object]:
        """Application-owned complete B2 join without altering single-block contracts."""
        if self.b11.narrative_core is None:
            raise BPipelineValidationError("B1.1 narrative core is missing")
        return {
            "schema_version": "b-pipeline-merged-v1",
            "pipeline_stage": "B2",
            **({"visual_style": self.visual_style} if self.visual_style == "profesional" else {}),
            "narrative_core": self.b11.narrative_core.model_dump(),
            "blocks": (
                _professional_plan(
                    [item for item in self.b2 if isinstance(item, ProfessionalB2Output)],
                    cta_groups=self.cta_groups,
                    cta_overrides=self.cta_overrides,
                )["blocks"]
                if self.visual_style == "profesional"
                else [item.blocks[0].model_dump() for item in self.b2 if item.blocks]
            ),
        }

    def merged_b21_output(self) -> dict[str, object]:
        """B2.1 is already a whole-video sparse patch; never re-emit B2."""
        if self.b21 is None:
            raise BPipelineValidationError("B2.1 output is missing")
        return self.b21.model_dump()

    def visual_plan(self) -> dict[str, object]:
        """Application-owned reviewed B2 plan, without camera-replanning."""
        if self.b11.narrative_core is None:
            raise BPipelineValidationError("B1.1 narrative core is missing")
        if self.visual_style == "profesional":
            professional = tuple(
                item for item in self.b2 if isinstance(item, ProfessionalB2Output)
            )
            if len(professional) != len(self.b2):
                raise BPipelineValidationError(
                    "Professional visual plan requires professional B2"
                )
            reviewed = _reviewed_professional_plan(
                professional, self.b21, cta_groups=self.cta_groups,
                cta_overrides=self.cta_overrides,
            )
            blocks = reviewed["blocks"]
        else:
            blocks = [
                result.blocks[0].model_dump() for result in self.b2 if result.blocks
            ]
        return {
            "schema_version": "b-pipeline-v1",
            **({"visual_style": self.visual_style} if self.visual_style == "profesional" else {}),
            "narrative_core": self.b11.narrative_core.model_dump(),
            "blocks": blocks,
        }


async def _call_stage[OutputT: BaseModel](
    stage: str,
    block_id: int | None,
    payload: dict[str, object],
    *,
    provider: StructuredTextProvider,
    model: str,
    prompt: str,
    instructions_suffix: str = "",
    output_type: type[OutputT],
    on_input: Callable[[str, int | None, dict[str, object]], None] | None,
    on_api_response: Callable[[str, int | None, object], None] | None,
    on_call_duration: Callable[[str, int | None, float], None] | None,
    input_image_url: str | None = None,
    previous_response_id: str | None = None,
    correction_feedback: str | None = None,
    on_response_id: Callable[[str], None] | None = None,
) -> OutputT:
    """Record exactly the submitted input and meter the returned API response."""
    continued = bool(
        previous_response_id and getattr(provider, "supports_response_continuation", False)
    )
    if on_input is not None:
        submitted = (
            {"_validation_correction": correction_feedback}
            if continued and correction_feedback
            else dict(payload)
        )
        if continued:
            submitted["_previous_response_id"] = previous_response_id
        on_input(stage, block_id, submitted)
    kwargs = {
        "model": model,
        "instructions": _instructions(prompt) + instructions_suffix,
        "input_text": (
            correction_feedback if continued and correction_feedback else _payload(payload)
        ),
        "output_type": output_type,
    }
    if input_image_url is not None and not continued:
        kwargs["input_image_url"] = input_image_url
    if continued:
        kwargs["previous_response_id"] = previous_response_id
    if stage == "B2" and getattr(provider, "supports_reasoning_effort_override", False):
        kwargs["reasoning_effort"] = settings.openai_b2_reasoning_effort
    started = perf_counter()
    try:
        metered = getattr(provider, "generate_structured_with_response", None)
        if callable(metered):
            output, response = await metered(**kwargs)
            if on_api_response is not None:
                on_api_response(stage, block_id, response)
            response_id = getattr(response, "id", None)
            if isinstance(response_id, str) and response_id and on_response_id is not None:
                on_response_id(response_id)
            return output
        return await provider.generate_structured(**kwargs)
    finally:
        if on_call_duration is not None:
            on_call_duration(stage, block_id, perf_counter() - started)



def validate_whole_beats(
    result: WholeB12Output,
    core: object,
    materialized: list[MaterializedBlock],
    script: str,
) -> list[B12Output]:
    """Validate every block and report all block-local failures together."""
    _require_success("B1.2", result.error)
    if result.narrative_core != core or not result.blocks:
        raise BPipelineValidationError("B1.2 altered the frozen narrative core")
    if len(result.blocks) != len(materialized):
        raise BPipelineValidationError("B1.2 changed the frozen block count")
    outputs: list[B12Output] = []
    errors: list[str] = []
    for actual, block in zip(result.blocks, materialized, strict=True):
        if actual.block_id != block.block_id:
            errors.append(
                f"B1.2 block {block.block_id} returned ID {actual.block_id} or changed block order"
            )
            continue
        part = B12Output(
            pipeline_stage="B1.2", block_id=block.block_id,
            beats=actual.beats, error=None,
        )
        try:
            actual.beats = validate_beats(part, block)
            if "".join(beat.text for beat in actual.beats) != block.text:
                raise BPipelineValidationError(
                    f"B1.2 block {block.block_id} does not reconstruct source exactly"
                )
        except (ValidationError, BPipelineValidationError) as error:
            errors.append(f"B1.2 block {block.block_id}: {error}")
            continue
        outputs.append(part)
    if errors:
        raise BPipelineValidationError(" || ".join(errors))
    if "".join(block.text for block in materialized) != script:
        raise BPipelineValidationError("Whole B1.2 input blocks do not reconstruct script")
    return outputs


def _failed_b12_block_ids(
    error: BaseException,
    current: WholeB12Output | None,
    materialized: list[MaterializedBlock],
    *,
    previous_targets: tuple[int, ...] = (),
) -> tuple[int, ...]:
    """Locate source block groups affected by a B1.2 validation failure."""
    valid_ids = {block.block_id for block in materialized}
    failed: set[int] = set()
    message = str(error)
    failed.update(
        int(match.group(1))
        for match in re.finditer(r"B1\.2 beat\s+(\d+)[A-Z]+", message, re.IGNORECASE)
    )
    failed.update(
        int(match.group(1))
        for match in re.finditer(r"B1\.2 block\s+(\d+)", message, re.IGNORECASE)
    )
    if isinstance(error, ValidationError):
        for detail in error.errors():
            location = detail.get("loc", ())
            try:
                blocks_index = location.index("blocks")
                block_offset = location[blocks_index + 1]
            except (ValueError, IndexError):
                continue
            if isinstance(block_offset, int):
                if previous_targets and 0 <= block_offset < len(previous_targets):
                    # Validation locations in a sparse retry are relative to its
                    # returned groups, not to the original whole-video block list.
                    failed.add(previous_targets[block_offset])
                elif 0 <= block_offset < len(materialized):
                    failed.add(materialized[block_offset].block_id)

    if current is not None and "block count" in message.lower():
        actual_ids = {block.block_id for block in current.blocks or []}
        failed.update(valid_ids - actual_ids)
        if not failed:
            failed.update(valid_ids)
    if current is not None and "block ids or order" in message.lower():
        actual_blocks = current.blocks or []
        for index, expected in enumerate(materialized):
            if index >= len(actual_blocks) or actual_blocks[index].block_id != expected.block_id:
                failed.add(expected.block_id)
    failed.intersection_update(valid_ids)
    if not failed and previous_targets:
        failed.update(previous_targets)
    if not failed:
        # If validation cannot associate a failure with one block, all source
        # blocks are the smallest safely identifiable correction scope.
        failed.update(valid_ids)
    return tuple(block_id for block_id in sorted(valid_ids) if block_id in failed)


def _b12_targeted_retry_feedback(
    error: BaseException,
    block_ids: tuple[int, ...],
    current: WholeB12Output,
    materialized: list[MaterializedBlock],
    core: object,
) -> str:
    """Ask B1.2 to re-segment only the source blocks that failed validation."""
    selected = set(block_ids)
    source_groups = [
        block.model_dump() for block in materialized if block.block_id in selected
    ]
    current_groups = [
        {"block_id": block.block_id, "beats": [beat.model_dump() for beat in block.beats]}
        for block in current.blocks or [] if block.block_id in selected
    ]
    return "\n".join((
        "VALIDATION FAILED: B1.2 targeted block correction",
        f"- Failure: {error}",
        "Required correction:",
        f"Correct only source block groups: {', '.join(map(str, block_ids))}.",
        "Return every beat for each named block, in source order, but do not return "
        "any unaffected block. Keep each beat at or below 40 words and 100 stripped "
        "characters. If a beat exceeds a limit, split that source passage into two "
        "or more consecutive beats and renumber the affected block's beat IDs "
        "sequentially from its A suffix.",
        "Preserve every source word exactly and do not move text across frozen B1.1 "
        "block boundaries. The application restores source punctuation and whitespace, "
        "then merges these groups into the previously accepted whole-video result and "
        "validates the complete reconstruction before continuing.",
        "Keep the narrative_core unchanged. Return the B1.2 success schema with "
        "pipeline_stage='B1.2', only the named block groups in blocks, and error=null.",
        "Narrative core:",
        json.dumps(core.model_dump() if isinstance(core, BaseModel) else core,
                   ensure_ascii=False, separators=(",", ":")),
        "Frozen source text and metadata for only the requested groups:",
        json.dumps(source_groups, ensure_ascii=False, separators=(",", ":")),
        "Previous beat segmentation for only those groups:",
        json.dumps(current_groups, ensure_ascii=False, separators=(",", ":")),
    ))


def merge_whole_b12_correction(
    current: WholeB12Output,
    correction: WholeB12Output,
    core: object,
    materialized: list[MaterializedBlock],
    block_ids: tuple[int, ...],
) -> tuple[WholeB12Output, list[str]]:
    """Merge valid corrected groups, retaining any block that still needs repair."""
    _require_success("B1.2", correction.error)
    if correction.narrative_core != core or not correction.blocks:
        raise BPipelineValidationError(
            "B1.2 targeted correction altered the narrative core or omitted all groups"
        )
    expected_ids = tuple(block.block_id for block in materialized)
    if tuple(block.block_id for block in current.blocks or []) != expected_ids:
        raise BPipelineValidationError(
            "B1.2 cannot merge a targeted correction into an incomplete whole-video result"
        )
    selected = set(block_ids)
    source_by_id = {block.block_id: block for block in materialized}
    merged_by_id = {
        block.block_id: block.model_copy(deep=True) for block in current.blocks or []
    }
    errors: list[str] = []
    returned_ids = [block.block_id for block in correction.blocks]
    duplicates = {block_id for block_id in returned_ids if returned_ids.count(block_id) > 1}
    for block_id in sorted(duplicates):
        errors.append(f"B1.2 block {block_id} was returned more than once")
    for block_id in block_ids:
        if block_id not in returned_ids:
            errors.append(f"B1.2 block {block_id} was omitted from its targeted correction")
    unexpected = set(returned_ids) - selected
    for block_id in sorted(unexpected):
        errors.append(f"B1.2 block {block_id} was returned outside the requested groups")
    if returned_ids != sorted(returned_ids):
        errors.append("B1.2 targeted correction returned its block groups out of order")

    seen: set[int] = set()
    for corrected in correction.blocks:
        block_id = corrected.block_id
        if block_id not in selected or block_id in seen:
            continue
        seen.add(block_id)
        source = source_by_id[block_id]
        part = B12Output(
            pipeline_stage="B1.2", block_id=block_id,
            beats=corrected.beats, error=None,
        )
        try:
            restored = validate_beats(part, source)
            if "".join(beat.text for beat in restored) != source.text:
                raise BPipelineValidationError(
                    f"B1.2 block {block_id} does not reconstruct source exactly"
                )
        except (ValidationError, BPipelineValidationError) as error:
            errors.append(f"B1.2 block {block_id}: {error}")
            continue
        merged_by_id[block_id] = WholeB12Block(block_id=block_id, beats=restored)

    merged = WholeB12Output(
        pipeline_stage="B1.2", narrative_core=current.narrative_core,
        blocks=[merged_by_id[block_id] for block_id in expected_ids], error=None,
    )
    return merged, errors


def validate_whole_professional_b2(
    result: WholeProfessionalB2Output,
    source: WholeProfessionalB2Input,
) -> list[ProfessionalB2Output]:
    """Verify the complete visual assignment before storing per-block outputs."""
    _require_success("B2", result.error)
    if result.narrative_core != source.narrative_core or not result.blocks:
        raise BPipelineValidationError("B2 altered the narrative core")
    if len(result.blocks) != len(source.blocks):
        raise BPipelineValidationError("B2 changed the frozen block count")
    outputs: list[ProfessionalB2Output] = []
    for block_index, (actual, expected) in enumerate(
        zip(result.blocks, source.blocks, strict=True)
    ):
        if actual.block_id != expected.block_id:
            raise BPipelineValidationError("B2 changed block IDs or order")
        supplied = B2Input(
            narrative_core=source.narrative_core,
            current_block_id=expected.block_id,
            blocks=(expected,),
        )
        part = ProfessionalB2Output(
            pipeline_stage="B2", narrative_core=result.narrative_core,
            blocks=[actual], error=None,
        )
        part = repair_frozen_b2_text(part, supplied)
        result.blocks[block_index] = part.blocks[0]
        validate_visuals(part, supplied, check_audio_run=False)
        outputs.append(part)
    overflows = _overlong_avatar_runs(outputs)
    if overflows:
        raise BPipelineValidationError(
            " || ".join(
                _avatar_run_error(*overflow, professional=True)
                for overflow in overflows
            )
        )
    _renumber_lettered_panels(outputs)
    _professional_grid_preflight(_professional_plan(outputs))
    return outputs


def validate_professional_b2_batch(
    result: ProfessionalB2BatchOutput,
    core: object,
    expected_blocks: list[B2InputBlock],
) -> list[ProfessionalB2Output]:
    """Validate one ordered B2 batch without losing full-plan context checks."""
    _require_success("B2", result.error)
    if result.narrative_core != core or not result.blocks:
        raise BPipelineValidationError("B2 batch altered the narrative core or omitted blocks")
    if len(result.blocks) != len(expected_blocks):
        raise BPipelineValidationError("B2 batch changed its frozen block count")
    outputs: list[ProfessionalB2Output] = []
    for index, (actual, expected) in enumerate(
        zip(result.blocks, expected_blocks, strict=True)
    ):
        if actual.block_id != expected.block_id:
            raise BPipelineValidationError("B2 batch altered block IDs or order")
        supplied = B2Input(
            narrative_core=core,
            current_block_id=expected.block_id,
            blocks=(expected,),
        )
        part = ProfessionalB2Output(
            pipeline_stage="B2", narrative_core=result.narrative_core,
            blocks=[actual], error=None,
        )
        part = repair_frozen_b2_text(part, supplied)
        result.blocks[index] = part.blocks[0]
        validate_visuals(part, supplied, check_audio_run=False)
        outputs.append(part)
    return outputs


def _professional_b2_failed_beat_groups(
    error: BaseException,
) -> dict[int, tuple[str, ...]] | None:
    """Extract the exact beat IDs in a failed professional audio run."""
    message = str(error)
    if not message.startswith("B2 consecutive audio=true beats "):
        return None
    groups: dict[int, list[str]] = {}
    for match in re.finditer(r"(?<!\d)(\d+[A-Z]+)\s+\(\d+\)", message):
        beat_id = match.group(1)
        block_id = int(re.match(r"\d+", beat_id).group())
        groups.setdefault(block_id, []).append(beat_id)
    return {block_id: tuple(beat_ids) for block_id, beat_ids in sorted(groups.items())} or None


def _professional_b2_failed_audio_runs(error: BaseException) -> tuple[tuple[str, ...], ...]:
    """Preserve separate overlong runs when several are reported together."""
    runs: list[tuple[str, ...]] = []
    for diagnostic in str(error).split(" || "):
        if not diagnostic.startswith("B2 consecutive audio=true beats "):
            continue
        beat_ids = tuple(
            match.group(1)
            for match in re.finditer(r"(?<!\d)(\d+[A-Z]+)\s+\(\d+\)", diagnostic)
        )
        if beat_ids:
            runs.append(beat_ids)
    return tuple(runs)


def _professional_b2_partial_feedback(
    error: BaseException,
    beat_groups: dict[int, tuple[str, ...]],
    previous: WholeProfessionalB2Output,
    *,
    correction_error: BaseException | None = None,
) -> str:
    """Ask for only the exact failing audio-run beat groups."""
    current_groups = []
    for block in previous.blocks or []:
        beat_ids = set(beat_groups.get(block.block_id, ()))
        beats = [beat.model_dump() for beat in block.beats if beat.beat_id in beat_ids]
        if beats:
            current_groups.append({"block_id": block.block_id, "beats": beats})
    failing_runs = _professional_b2_failed_audio_runs(error)
    named_groups = "; ".join(
        f"run {index}: {', '.join(run)}"
        for index, run in enumerate(failing_runs, 1)
    ) or "; ".join(
        f"block {block_id}: {', '.join(beat_ids)}"
        for block_id, beat_ids in beat_groups.items()
    )
    return "\n".join((
        "VALIDATION FAILED: B2 profesional whole video",
        "Validator diagnostics:",
        f"- Failure: {error}",
        "Required correction:",
        f"Failing consecutive beat run(s): {named_groups}.",
        "For each reported run, change the minimum number of discretionary beats "
        "needed so every remaining consecutive audio=true segment is at most 120 "
        "characters. Use a faithful media_image with audio=false, a nonempty English "
        "prompt, and video_prompt=null for a break; use media_video only if its motion "
        "materially improves the scene, with a valid video_prompt.",
        "Leave every other beat's visual/audio fields exactly as supplied.",
        "Return only the beat entries whose visual fields you changed to create "
        "those breaks. You may return a subset of the listed IDs; omit unchanged "
        "beats. Each blocks entry contains only block_id and a beats array of the "
        "changed beat IDs in source order. Do not return complete blocks or the full plan.",
        "Preserve each returned beat's ID, text, and type. The application merges "
        "only these beat visual assignments into the latest plan, retaining every "
        "other beat and all earlier accepted corrections unchanged.",
        "Respect the mandatory original-avatar anchor on the final beat of every "
        "close block. The merged whole-video plan will be checked again for all "
        "120-character audio=true runs.",
        "Return the B2 correction schema with pipeline_stage='B2', the unchanged "
        "narrative_core, the listed beat groups under blocks, and error=null.",
        *(
            (f"The previous patch did not pass validation: {correction_error}. "
             "Return a valid patch for the same current run groups.",)
            if correction_error is not None else ()
        ),
        "Current versions of only the failing beats:",
        json.dumps(current_groups, ensure_ascii=False, separators=(",", ":")),
    ))


def _professional_b2_batch_retry_feedback(
    error: BaseException,
    *,
    batch_number: int,
    total_batches: int,
    block_ids: tuple[int, ...],
    previous: ProfessionalB2BatchOutput | None = None,
) -> str:
    """Retry only the current batch after a non-audio validation failure."""
    ids = ", ".join(map(str, block_ids))
    lines = [
        f"VALIDATION FAILED: B2 batch {batch_number}/{total_batches}",
        f"- Failure: {error}",
        "Required correction:",
        f"Correct only this batch's block IDs: {ids}.",
        "Return each listed block completely, with all beats in source order. "
        "Do not return prior accepted batches or future blocks.",
        "Preserve B1.1 metadata and every frozen beat ID, text, order, and type. "
        "Correct only the B2 visual fields that failed validation.",
        "The application will keep earlier accepted batches unchanged and validate "
        "the accumulated plan again before requesting the next batch.",
        "Return the B2 batch schema with pipeline_stage='B2', unchanged "
        "narrative_core, only the listed blocks, and error=null.",
    ]
    if previous is not None and previous.blocks:
        lines.extend((
            "Current response for this batch:",
            json.dumps(
                [block.model_dump() for block in previous.blocks],
                ensure_ascii=False, separators=(",", ":"),
            ),
        ))
    return "\n".join(lines)


def _professional_b2_invalid_input_feedback(
    error: BaseException,
    *,
    image_supplied: bool,
    batch_number: int,
    total_batches: int,
    block_ids: tuple[int, ...],
) -> str:
    """Correct a model-reported input error after local source checks passed."""
    lines = [
        "VALIDATION FAILED: B2 professional input diagnosis",
        f"- Your previous response returned INVALID_UPSTREAM_INPUT: {error}",
        "- The application has already verified the complete ordered B1.1/B1.2 "
        "source against the authoritative script.",
        f"- Batch {batch_number}/{total_batches} uses validated source blocks "
        f"{', '.join(map(str, block_ids))}, and their span anchors match each block edge.",
        "Required correction:",
        "The upstream input is valid. Do not return INVALID_UPSTREAM_INPUT or any "
        "error object. Continue the visual-direction task and return only the "
        f"complete current batch blocks {', '.join(map(str, block_ids))}.",
        "Do not reinterpret separator whitespace at block boundaries as an "
        "ambiguous source association; the application already resolved those "
        "boundaries and validated exact script reconstruction.",
    ]
    if image_supplied:
        lines.append(
            "The original-avatar reference image was supplied with the initial "
            "Responses API call and remains available through this response continuation."
        )
    return "\n".join(lines)


def merge_professional_b2_correction(
    base: WholeProfessionalB2Output,
    correction: ProfessionalB2CorrectionOutput,
    source: WholeProfessionalB2Input,
    beat_groups: dict[int, tuple[str, ...]],
) -> WholeProfessionalB2Output:
    """Merge only the explicitly requested beat visuals into the full plan."""
    _require_success("B2", correction.error)
    if correction.narrative_core != source.narrative_core or not correction.blocks:
        raise BPipelineValidationError(
            "B2 targeted correction altered the narrative core or omitted beat groups"
        )
    corrected_by_id: dict[int, dict[str, ProfessionalVisualBeat]] = {}
    for group in correction.blocks:
        if group.block_id in corrected_by_id:
            raise BPipelineValidationError(
                f"B2 targeted correction duplicated block {group.block_id}"
            )
        corrected_by_id[group.block_id] = {}
        for beat in group.beats:
            if beat.beat_id in corrected_by_id[group.block_id]:
                raise BPipelineValidationError(
                    f"B2 targeted correction duplicated beat {beat.beat_id}"
                )
            corrected_by_id[group.block_id][beat.beat_id] = beat
    unexpected = sorted(set(corrected_by_id) - set(beat_groups))
    if unexpected:
        raise BPipelineValidationError(
            "B2 targeted correction returned unexpected block IDs "
            + ", ".join(map(str, unexpected))
        )
    expected_block_order = tuple(
        block_id for block_id in beat_groups if block_id in corrected_by_id
    )
    if tuple(corrected_by_id) != expected_block_order:
        raise BPipelineValidationError(
            "B2 targeted correction returned beat groups out of source order"
        )

    source_by_id = {block.block_id: block for block in source.blocks}
    result = base.model_copy(deep=True)
    result_index_by_id = {
        block.block_id: index for index, block in enumerate(result.blocks or [])
    }
    for block_id, returned in corrected_by_id.items():
        target_beat_ids = beat_groups[block_id]
        expected = source_by_id.get(block_id)
        result_index = result_index_by_id.get(block_id)
        if expected is None or result_index is None:
            raise BPipelineValidationError(
                f"B2 targeted correction refers to unknown block {block_id}"
            )
        if not set(returned) <= set(target_beat_ids):
            extra = sorted(set(returned) - set(target_beat_ids))
            raise BPipelineValidationError(
                f"B2 targeted correction returned non-target beats "
                f"{', '.join(extra)} for block {block_id}"
            )
        target_order = tuple(beat_id for beat_id in target_beat_ids if beat_id in returned)
        if tuple(returned) != target_order:
            raise BPipelineValidationError(
                f"B2 targeted correction beats are out of source order in block {block_id}"
            )
        source_beats = {beat.beat_id: beat for beat in expected.beats}
        current_block = result.blocks[result_index]
        merged_beats: list[ProfessionalVisualBeat] = []
        for visual in current_block.beats:
            upstream = source_beats.get(visual.beat_id)
            if upstream is None:
                raise BPipelineValidationError(
                    f"B2 base plan contains unknown beat {visual.beat_id}"
                )
            patch = returned.get(visual.beat_id)
            merged_beats.append(
                visual.model_copy(update={
                    "beat_id": upstream.beat_id,
                    "text": upstream.text,
                    "beat_type": upstream.beat_type,
                    **({
                        "visual_type": patch.visual_type,
                        "prompt": patch.prompt,
                        "video_prompt": patch.video_prompt,
                        "reference": patch.reference,
                        "audio": patch.audio,
                    } if patch is not None else {}),
                })
            )
        block_payload = current_block.model_dump()
        block_payload["beats"] = [beat.model_dump() for beat in merged_beats]
        result.blocks[result_index] = ProfessionalB2Block.model_validate(block_payload)
    return result


async def run_b_pipeline(
    script: str,
    *,
    provider: StructuredTextProvider,
    model: str | None = None,
    max_parallel_calls: int = 8,
    visual_style: Literal["casero", "profesional"] = "casero",
    enable_casero_cta: bool = False,
    avatar_image: bytes | None = None,
    ebook_image: bytes | None = None,
    previous_cta_groups: tuple[tuple[str, ...], ...] | None = None,
    previous_cta_overrides: dict[str, ProfessionalCTAVisualBeat] | None = None,
    start_from: Literal["B1.1", "B1.2", "B2"] = "B1.1",
    stop_after: Literal["B1.1", "B1.2", "B2", "B2.1"] = "B2",
    previous_b11: B11Output | None = None,
    previous_b12: tuple[B12Output, ...] | None = None,
    previous_b2: tuple[B2Output, ...] | None = None,
    retry_b2_block_ids: frozenset[int] | None = None,
    previous_partial_b12: dict[int, B12Output] | None = None,
    previous_partial_b2: dict[int, B2Output | ProfessionalB2Output] | None = None,
    previous_b2_correction_counts: dict[int, int] | None = None,
    previous_b2_response_ids: dict[int, str] | None = None,
    retry_b2_feedback: dict[int, str] | None = None,
    on_input: Callable[[str, int | None, dict[str, object]], None] | None = None,
    on_output: Callable[[str, int | None, dict[str, object]], None] | None = None,
    on_rejected_output: Callable[[int, str, dict[str, object]], None] | None = None,
    on_rejected_b12_output: (
        Callable[[int, int, str, dict[str, object]], None] | None
    ) = None,
    on_rejected_b2_output: (
        Callable[[int, int, str, dict[str, object]], None] | None
    ) = None,
    on_rejected_b21_output: (
        Callable[[int, str, dict[str, object]], None] | None
    ) = None,
    on_api_response: Callable[[str, int | None, object], None] | None = None,
    on_call_duration: Callable[[str, int | None, float], None] | None = None,
    on_stage_duration: Callable[[str, float], None] | None = None,
    on_stage_complete: (
        Callable[[str, int, dict[str, object] | None], None] | None
    ) = None,
) -> BPipelineResult:
    """Run each bot with its current prompt file and deterministic join order."""
    if model is None:
        model = settings.openai_model
    if not script or not script.strip():
        raise BPipelineValidationError("Authoritative script must not be empty")
    if max_parallel_calls < 1:
        raise ValueError("max_parallel_calls must be at least 1")
    if visual_style not in {"casero", "profesional"}:
        raise ValueError("Unknown B2 visual style")
    image_url: str | None = None
    if stop_after in {"B2", "B2.1"}:
        if visual_style == "profesional" and not avatar_image:
            raise ValueError("Professional B2 requires the selected avatar PNG")
        if avatar_image:
            image_url = (
                "data:image/png;base64,"
                + base64.b64encode(avatar_image).decode("ascii")
            )
    if retry_b2_block_ids is not None and start_from != "B2":
        raise ValueError("B2 block retries require start_from='B2'")
    if start_from not in {"B1.1", "B1.2", "B2"}:
        raise ValueError(f"Unknown start stage: {start_from}")
    if stop_after not in {"B1.1", "B1.2", "B2", "B2.1"}:
        raise ValueError(f"Unknown stop stage: {stop_after}")
    if stop_after == "B2.1" and visual_style != "profesional":
        raise ValueError("B2.1 is available only for professional visual style")
    stage_order = ("B1.1", "B1.2", "B2", "B2.1")
    if stage_order.index(stop_after) < stage_order.index(start_from):
        raise ValueError("--stop cannot be earlier than --from")

    b11_response_id: str | None = None

    def remember_b11_response(response_id: str) -> None:
        nonlocal b11_response_id
        b11_response_id = response_id

    async def call_b11(
        feedback: str = "", previous_response_id: str | None = None,
    ) -> B11Output:
        return await _call_stage(
            "B1.1",
            None,
            {"plain_script_for_recording": script},
            provider=provider,
            model=model,
            prompt="b1_1.md",
            instructions_suffix="\n\n" + feedback if feedback else "",
            output_type=B11Output,
            previous_response_id=previous_response_id,
            correction_feedback=feedback or None,
            on_response_id=remember_b11_response,
            on_input=on_input,
            on_api_response=on_api_response,
            on_call_duration=on_call_duration,
        )

    async def call_cta() -> None:
        started = perf_counter()
        payload = {"script_for_recording": script}

        async def attempt(suffix: str = "") -> CTAPlacement:
            return await _call_stage(
                "CTA", None, payload, provider=provider, model=model,
                prompt="cta.md", instructions_suffix=suffix,
                output_type=CTAPlacement, on_input=on_input,
                on_api_response=on_api_response, on_call_duration=on_call_duration,
            )

        try:
            try:
                found = await attempt()
                position = validate_placement(script, found)
            except ValueError as error:
                found = await attempt(
                    "\n\nYour previous CTA anchor pair failed deterministic validation: "
                    + str(error) + ". Return exact unique adjacent spoken sentences."
                )
                position = validate_placement(script, found)
            if on_output is not None:
                on_output("CTA", None, {
                    **found.model_dump(), "insert_after_character": position,
                })
        finally:
            if on_stage_duration is not None:
                on_stage_duration("CTA", perf_counter() - started)

    cta_task: asyncio.Task[None] | None = None
    if start_from == "B1.1":
        if enable_casero_cta and visual_style == "casero":
            cta_task = asyncio.create_task(call_cta())
        b11_started = perf_counter()
        try:
            feedback = ""
            previous_response_id: str | None = None
            for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
                candidate: B11Output | None = None
                try:
                    candidate = await call_b11(feedback, previous_response_id)
                    _require_success("B1.1", candidate.error)
                    materialized = materialize_blocks(script, candidate.blocks)
                    b11 = candidate
                    break
                except (ValidationError, BPipelineValidationError, RuntimeError) as error:
                    if isinstance(error, RuntimeError) and not is_schema_failure(error):
                        raise
                    if candidate is not None and candidate.error is not None:
                        if candidate.error.code == "UNSUPPORTED_INPUT":
                            raise
                    rejected = (
                        candidate.model_dump()
                        if candidate is not None else {
                            "unparsed_response": True,
                            "validation_error": str(error),
                        }
                    )
                    if on_rejected_output is not None:
                        on_rejected_output(attempt, str(error), rejected)
                    if attempt > MAX_VALIDATION_RETRIES:
                        raise BPipelineValidationError(
                            f"B1.1 failed validation after {MAX_VALIDATION_RETRIES} "
                            f"retries: {error}"
                        ) from error
                    previous_response_id = (
                        b11_response_id
                        if getattr(provider, "supports_response_continuation", False)
                        else None
                    )
                    feedback = validation_feedback(
                        "B1.1", error, previous=candidate.model_dump() if candidate else None,
                        continued=previous_response_id is not None,
                        source_script=script,
                        instructions=(
                            "Correct the specified invalid fields. Retain valid narrative "
                            "metadata and the intended block structure, except when the "
                            "validator proves that structure is invalid. Copy anchors "
                            "verbatim from the original script, ensure consecutive IDs, "
                            "and ensure precisely one full, ordered tiling without missing "
                            "or duplicated source characters."
                        ),
                    )
            if on_output is not None:
                on_output("B1.1", None, b11.model_dump())
        finally:
            if on_stage_duration is not None:
                on_stage_duration("B1.1", perf_counter() - b11_started)
        if on_stage_complete is not None:
            on_stage_complete("B1.1", len(materialized), None)
        if cta_task is not None:
            await asyncio.shield(cta_task)
        if stop_after == "B1.1":
            return BPipelineResult(b11=b11, b12=(), b2=())
    else:
        if previous_b11 is None:
            raise ValueError(f"Starting from {start_from} requires a validated B1.1 checkpoint")
        b11 = previous_b11
        _require_success("B1.1 checkpoint", b11.error)
        if b11.narrative_core is None or b11.blocks is None:
            raise BPipelineValidationError("B1.1 checkpoint is missing required fields")
        materialized = materialize_blocks(script, b11.blocks)

    semaphore = asyncio.Semaphore(max_parallel_calls)
    if start_from == "B2":
        if previous_b12 is None:
            raise ValueError("Starting from B2 requires validated B1.2 checkpoints")
        b12_results = list(previous_b12)
        if len(b12_results) != len(materialized):
            raise BPipelineValidationError("B1.2 checkpoint block count does not match B1.1")
        for expected_id, output, block in zip(
            range(1, len(materialized) + 1), b12_results, materialized, strict=True
        ):
            if output.block_id != expected_id:
                raise BPipelineValidationError("B1.2 checkpoint block IDs are not consecutive")
            validate_beats(output, block)
    else:
        # One complete B1.2 call calibrates narrative pulses against the full arc.
        # Incomplete old per-block checkpoints cannot be stitched into a global plan.
        b12_input = WholeB12Input(
            plain_script_for_recording=script,
            narrative_core=b11.narrative_core,
            blocks=list(materialized),
        )
        b12_response_id: str | None = None

        def remember_b12(response_id: str) -> None:
            nonlocal b12_response_id
            b12_response_id = response_id

        b12_started = perf_counter()
        try:
            feedback = ""
            previous_response_id: str | None = None
            correction_block_ids: tuple[int, ...] | None = None
            b12_candidate: WholeB12Output | None = None
            attempt = 0
            full_retry_count = 0
            block_retry_counts: dict[int, int] = {}
            while True:
                attempt += 1
                candidate: WholeB12Output | None = None
                try:
                    if correction_block_ids is None:
                        request_payload = b12_input.model_dump()
                        correction_feedback = None
                    else:
                        if b12_candidate is None:
                            raise BPipelineValidationError(
                                "B1.2 targeted correction has no complete base result"
                            )
                        selected = set(correction_block_ids)
                        request_payload = {
                            "narrative_core": b11.narrative_core.model_dump(),
                            "blocks": [
                                block.model_dump() for block in materialized
                                if block.block_id in selected
                            ],
                        }
                        correction_feedback = feedback
                    candidate = await _call_stage(
                        "B1.2", None, request_payload,
                        provider=provider, model=model, prompt="b1_2.md",
                        instructions_suffix="\n\n" + feedback if feedback else "",
                        correction_feedback=correction_feedback,
                        previous_response_id=previous_response_id,
                        on_response_id=remember_b12,
                        output_type=WholeB12Output,
                        on_input=on_input,
                        on_api_response=on_api_response,
                        on_call_duration=on_call_duration,
                    )
                    if correction_block_ids is None:
                        b12_candidate = candidate.model_copy(deep=True)
                        b12_results = validate_whole_beats(
                            b12_candidate, b11.narrative_core, materialized, script,
                        )
                    else:
                        if b12_candidate is None:
                            raise BPipelineValidationError(
                                "B1.2 targeted correction has no complete base result"
                            )
                        b12_candidate, merge_errors = merge_whole_b12_correction(
                            b12_candidate, candidate, b11.narrative_core,
                            materialized, correction_block_ids,
                        )
                        try:
                            b12_results = validate_whole_beats(
                                b12_candidate, b11.narrative_core, materialized, script,
                            )
                        except (ValidationError, BPipelineValidationError) as error:
                            if merge_errors:
                                raise BPipelineValidationError(
                                    " || ".join((*merge_errors, str(error)))
                                ) from error
                            raise
                        if merge_errors:
                            raise BPipelineValidationError(" || ".join(merge_errors))
                    break
                except (ValidationError, BPipelineValidationError, RuntimeError) as error:
                    if isinstance(error, RuntimeError) and not is_schema_failure(error):
                        raise
                    if candidate is not None and candidate.error is not None:
                        raise
                    rejected = candidate.model_dump() if candidate is not None else {
                        "unparsed_response": True, "validation_error": str(error),
                    }
                    if on_rejected_b12_output is not None:
                        on_rejected_b12_output(0, attempt, str(error), rejected)
                    previous_response_id = (
                        b12_response_id
                        if getattr(provider, "supports_response_continuation", False)
                        else None
                    )
                    if b12_candidate is None and candidate is not None:
                        b12_candidate = candidate.model_copy(deep=True)
                    if b12_candidate is None:
                        # A response that could not be parsed has no valid full plan
                        # to merge, so retain a small complete-output retry budget.
                        if full_retry_count >= MAX_VALIDATION_RETRIES:
                            raise BPipelineValidationError(
                                "Whole-video B1.2 failed after "
                                f"{full_retry_count} complete-output retries: {error}"
                            ) from error
                        full_retry_count += 1
                        correction_block_ids = None
                        feedback = validation_feedback(
                            "B1.2 whole video", error,
                            previous=candidate.model_dump() if candidate else None,
                            continued=previous_response_id is not None,
                            instructions=(
                                "Retry the complete whole-video beat segmentation because "
                                "no complete structured result was available to merge. "
                                "Preserve every B1.1 block and source word. Respect the "
                                "40-word and 100-character maximums."
                            ),
                        )
                    else:
                        correction_block_ids = _failed_b12_block_ids(
                            error, b12_candidate, materialized,
                            previous_targets=correction_block_ids or (),
                        )
                        exhausted = tuple(
                            block_id for block_id in correction_block_ids
                            if block_retry_counts.get(block_id, 0) >= MAX_VALIDATION_RETRIES
                        )
                        if exhausted:
                            raise BPipelineValidationError(
                                "B1.2 block groups failed after "
                                f"{MAX_VALIDATION_RETRIES} targeted retries each: "
                                f"{', '.join(map(str, exhausted))}: {error}"
                            ) from error
                        for block_id in correction_block_ids:
                            block_retry_counts[block_id] = (
                                block_retry_counts.get(block_id, 0) + 1
                            )
                        feedback = _b12_targeted_retry_feedback(
                            error, correction_block_ids, b12_candidate,
                            materialized, b11.narrative_core,
                        )
            if b12_candidate is None:
                raise BPipelineValidationError("B1.2 produced no complete merged result")
            if on_output is not None:
                on_output("B1.2", None, b12_candidate.model_dump())
                for item in b12_results:
                    on_output("B1.2", item.block_id, item.model_dump())
        finally:
            if on_stage_duration is not None:
                on_stage_duration("B1.2", perf_counter() - b12_started)
        if on_stage_complete is not None:
            partial = BPipelineResult(b11=b11, b12=tuple(b12_results), b2=())
            on_stage_complete("B1.2", len(b12_results), partial.merged_b12_output())
    if stop_after == "B1.2":
        return BPipelineResult(b11=b11, b12=tuple(b12_results), b2=())

    # The detector sees the complete B1.2 merge in parallel with B2 workers.
    cta_merged_b12 = BPipelineResult(
        b11=b11, b12=tuple(b12_results), b2=(),
    ).merged_b12_output()
    if previous_cta_groups is not None:
        from .professional_cta import ProfessionalCTADetection
        from .professional_cta_workflow import validate_cta_detection

        validate_cta_detection(
            ProfessionalCTADetection(
                cta_groups=[list(group) for group in previous_cta_groups],
            ),
            cta_merged_b12,
        )
    cta_detection_task = (
        asyncio.create_task(detect_professional_ctas(
            cta_merged_b12, provider=provider, model=model, semaphore=semaphore,
            on_input=on_input, on_output=on_output,
            on_api_response=on_api_response, on_call_duration=on_call_duration,
        ))
        if (
            visual_style == "profesional" and ebook_image is not None
            and previous_cta_groups is None
        ) else None
    )

    # One shared retry budget per B2 block. Global avatar-run repairs consume
    # the SAME budget as local schema and source-integrity corrections.
    b2_correction_counts = dict(previous_b2_correction_counts or {})
    b2_call_numbers: dict[int, int] = {
        block_id: count + 1 for block_id, count in b2_correction_counts.items()
    }
    b2_last_response_ids = dict(previous_b2_response_ids or {})

    async def b2_one(
        meta: BlockMeta, block: MaterializedBlock, b12: B12Output,
        correction_suffix: str = "",
    ) -> B2Output | ProfessionalB2Output:
        beats = validate_beats(b12, block)
        b2_input = B2Input(
            narrative_core=b11.narrative_core,
            current_block_id=block.block_id,
            blocks=(
                B2InputBlock(
                    block_id=meta.block_id,
                    type=meta.type,
                    emotional_entry=meta.emotional_entry,
                    emotional_exit=meta.emotional_exit,
                    span=meta.span,
                    beats=beats,
                ),
            ),
        )
        block_id = block.block_id
        if correction_suffix:
            if b2_correction_counts.get(block_id, 0) >= MAX_VALIDATION_RETRIES:
                raise BPipelineValidationError(
                    f"B2 block {block_id} exhausted its validation retry budget"
                )
            b2_correction_counts[block_id] = b2_correction_counts.get(block_id, 0) + 1

        def remember_response(response_id: str) -> None:
            b2_last_response_ids[block_id] = response_id

        async def call_b2(
            feedback: str = "", previous_response_id: str | None = None,
        ) -> B2Output | ProfessionalB2Output:
            async with semaphore:
                return await _call_stage(
                    "B2",
                    block_id,
                    b2_input.model_dump(),
                    provider=provider,
                    model=model,
                    prompt="b2_professional.md" if visual_style == "profesional" else "b2.md",
                    instructions_suffix="\n\n" + feedback if feedback else "",
                    output_type=(
                        ProfessionalB2Output if visual_style == "profesional" else B2Output
                    ),
                    input_image_url=image_url,
                    correction_feedback=feedback or None,
                    previous_response_id=previous_response_id,
                    on_response_id=remember_response,
                    on_input=on_input,
                    on_api_response=on_api_response,
                    on_call_duration=on_call_duration,
                )

        feedback = correction_suffix
        previous_response_id = (
            b2_last_response_ids.get(block_id)
            if correction_suffix and getattr(provider, "supports_response_continuation", False)
            else None
        )
        while True:
            b2_call_numbers[block_id] = b2_call_numbers.get(block_id, 0) + 1
            attempt = b2_call_numbers[block_id]
            candidate: B2Output | ProfessionalB2Output | None = None
            try:
                candidate = await call_b2(feedback, previous_response_id)
                if candidate.error is not None:
                    _require_success("B2", candidate.error)
                candidate = repair_frozen_b2_text(candidate, b2_input)
                validate_visuals(candidate, b2_input)
                if on_output is not None:
                    on_output("B2", block_id, candidate.model_dump())
                return candidate
            except (ValidationError, BPipelineValidationError, RuntimeError) as error:
                if isinstance(error, RuntimeError) and not is_schema_failure(error):
                    raise
                rejected = candidate.model_dump() if candidate is not None else {
                    "unparsed_response": True,
                    "schema_errors": (
                        json.loads(error.json(include_url=False))
                        if isinstance(error, ValidationError) else str(error)
                    ),
                }
                if on_rejected_b2_output is not None:
                    on_rejected_b2_output(block_id, attempt, str(error), rejected)
                if candidate is not None and candidate.error is not None:
                    # The frozen B1.2 source must be repaired upstream, not by B2.
                    raise
                if b2_correction_counts.get(block_id, 0) >= MAX_VALIDATION_RETRIES:
                    raise BPipelineValidationError(
                        f"B2 block {block_id} failed validation after "
                        f"{MAX_VALIDATION_RETRIES} retries: {error}"
                    ) from error
                b2_correction_counts[block_id] = b2_correction_counts.get(block_id, 0) + 1
                previous_response_id = (
                    b2_last_response_ids.get(block_id)
                    if getattr(provider, "supports_response_continuation", False)
                    else None
                )
                targeted_run_repair = ""
                if (
                    visual_style == "profesional"
                    and isinstance(error, BPipelineValidationError)
                    and str(error).startswith("B2 consecutive audio=true beats")
                    and isinstance(candidate, ProfessionalB2Output)
                ):
                    overflow = _overlong_avatar_run((candidate,))
                    if overflow is not None:
                        run, total = overflow
                        preferred = _preferred_avatar_run_break(
                            (candidate,),
                            run,
                            total,
                            professional=True,
                        )
                        if preferred is not None:
                            if preferred.sufficient:
                                split_detail = (
                                    "This single cut is sufficient: it leaves adjacent "
                                    f"audio=true segments of {preferred.left_characters} and "
                                    f"{preferred.right_characters} characters, both within "
                                    f"the {_MAX_CONSECUTIVE_AVATAR_CHARACTERS}-character limit. "
                                )
                            else:
                                split_detail = (
                                    "This is the most balanced first cut, but another "
                                    "audio=false break may still be required afterward. "
                                )
                            targeted_run_repair = (
                                " This validation failure is specifically the professional "
                                "audio-run limit. Change beat "
                                f"{preferred.beat_id} to a faithful audio=false media_image "
                                "or media_video treatment and update its prompt, video_prompt "
                                "and reference fields coherently for that carrier. "
                                + split_detail
                                + "Keep every other valid beat unchanged unless an additional "
                                "break is still required."
                            )

                feedback = validation_feedback(
                    "B2 " + visual_style, error, block_id=block_id,
                    previous=candidate.model_dump() if candidate else None,
                    continued=previous_response_id is not None,
                    instructions=(
                        "Repair only this B2 block. Preserve the narrative core, "
                        "block metadata, all frozen beats, beat IDs, and narration exactly. "
                        "Fix the invalid visual assignments, prompts, video_prompt, "
                        "descriptions, reference/audio flags and any mandatory "
                        "presenter anchors according to the selected B2 schema. "
                        "For professional style only the unchanged avatar and avatar_media may "
                        "have audio=true. All media_image and media_video must have "
                        "audio=false. A generated B-roll image of the presenter must "
                        "use their identity reference and a different physical location "
                        "from their original narration room; never generate a new "
                        "talking-avatar image or any B-roll direct-to-camera speech. "
                        "Do not change valid decisions unnecessarily. "
                        + (
                            "For professional B2, only a reused avatar may have a null "
                            "prompt; all other visuals require a nonempty prompt. "
                            "media_image and media_video ALWAYS require audio=false; "
                            "media_video needs a nonempty motion video_prompt. "
                            "The LAST beat of a close must reuse the original avatar "
                            "without regeneration."
                            if visual_style == "profesional" else ""
                        )
                        + targeted_run_repair
                    ),
                )

    retry_ids = set(retry_b2_block_ids or ())
    if retry_ids and previous_partial_b2:
        raise ValueError("Cannot combine selective B2 retry with partial recovery")
    if retry_ids:
        if previous_b2 is None or len(previous_b2) != len(materialized):
            raise ValueError("B2 block retries require a complete validated B2 checkpoint")
        previous_by_id = {item.blocks[0].block_id: item for item in previous_b2}
        expected_ids = set(range(1, len(materialized) + 1))
        if set(previous_by_id) != expected_ids or not retry_ids <= expected_ids:
            raise BPipelineValidationError("B2 retry checkpoint block IDs are invalid")
    else:
        previous_by_id = {}

    async def b2_selected(
        meta: BlockMeta, block: MaterializedBlock, b12: B12Output,
    ) -> B2Output | ProfessionalB2Output:
        if retry_ids and meta.block_id in retry_ids and retry_b2_feedback:
            previous = previous_by_id[meta.block_id]
            error = BPipelineValidationError(retry_b2_feedback[meta.block_id])
            if on_rejected_b2_output is not None:
                on_rejected_b2_output(
                    meta.block_id, b2_call_numbers.get(meta.block_id, 1),
                    str(error), previous.model_dump(),
                )
            suffix = validation_feedback(
                "B2 " + visual_style, error, block_id=meta.block_id,
                previous=previous.model_dump(),
                continued=bool(b2_last_response_ids.get(meta.block_id))
                and getattr(provider, "supports_response_continuation", False),
                instructions=(
                    "Repair the failed image-grid continuity numbering for ONLY "
                    "this B2 block. Keep frozen beats and all other valid visuals, "
                    "while correcting the visual descriptions/prompts whose "
                    "numbering caused the image preflight error."
                ),
            )
            return await b2_one(meta, block, b12, suffix)
        return await b2_one(meta, block, b12)

    async def b2_whole_professional(
        downstream_feedback: str = "",
    ) -> list[ProfessionalB2Output]:
        # One Responses API conversation plans four blocks at a time, in order.
        reserved_groups = (
            await cta_detection_task if cta_detection_task is not None
            else previous_cta_groups
        )
        frozen_blocks = [
            B2InputBlock(
                block_id=meta.block_id, type=meta.type,
                emotional_entry=meta.emotional_entry, emotional_exit=meta.emotional_exit,
                span=meta.span, beats=validate_beats(b12, block),
            )
            for meta, block, b12 in zip(b11.blocks, materialized, b12_results, strict=True)
        ]
        if "".join(block.text for block in materialized) != script:
            raise BPipelineValidationError(
                "Professional B2 source blocks do not reconstruct the authoritative script"
            )
        for meta, block in zip(b11.blocks, materialized, strict=True):
            if (
                not block.text.lstrip().startswith(meta.span.first_words)
                or not block.text.rstrip().endswith(meta.span.last_words)
            ):
                raise BPipelineValidationError(
                    f"Professional B2 block {block.block_id} does not match its frozen B1.1 anchors"
                )
        if "".join(
            beat.text for block in frozen_blocks for beat in block.beats
        ) != script:
            raise BPipelineValidationError(
                "Professional B2 beats do not reconstruct the authoritative script"
            )
        cta_payload = (
            [list(group) for group in reserved_groups]
            if reserved_groups is not None else None
        )
        accepted: list[ProfessionalB2Output] = []
        b2_response_id: str | None = None
        api_attempt = 0

        def remember_b2_response(response_id: str) -> None:
            nonlocal b2_response_id
            b2_response_id = response_id

        total_batches = (
            len(frozen_blocks) + _PROFESSIONAL_B2_BATCH_SIZE - 1
        ) // _PROFESSIONAL_B2_BATCH_SIZE
        for batch_number, offset in enumerate(
            range(0, len(frozen_blocks), _PROFESSIONAL_B2_BATCH_SIZE), start=1
        ):
            batch_blocks = frozen_blocks[
                offset:offset + _PROFESSIONAL_B2_BATCH_SIZE
            ]
            batch_ids = tuple(block.block_id for block in batch_blocks)
            batch_input = ProfessionalB2BatchInput(
                narrative_core=b11.narrative_core,
                batch_number=batch_number,
                total_batches=total_batches,
                blocks=batch_blocks,
                cta_groups=cta_payload,
            )
            prefix_blocks = frozen_blocks[:offset + len(batch_blocks)]
            prefix_source = WholeProfessionalB2Input(
                narrative_core=b11.narrative_core,
                blocks=prefix_blocks,
                cta_groups=cta_payload,
            )
            batch_note = (
                f"B2 sequential batch {batch_number}/{total_batches}. Use the narrative "
                "core, this batch, and earlier conversation turns as context. "
                f"Return only current block IDs {', '.join(map(str, batch_ids))}. "
                "Earlier batches in this Responses API conversation have been validated "
                "and accepted; preserve their visual decisions."
            )
            if downstream_feedback and (
                bool(set(batch_ids) & retry_ids) or (not retry_ids and batch_number == 1)
            ):
                batch_note += "\n\n" + downstream_feedback

            partial_base: WholeProfessionalB2Output | None = None
            partial_beat_groups: dict[int, tuple[str, ...]] | None = None
            feedback = batch_note
            validation_retries = 0
            upstream_input_retries = 0
            server_error_retries = 0
            batch_attempt = 0
            while True:
                batch_attempt += 1
                api_attempt += 1
                candidate: WholeProfessionalB2Output | None = None
                raw_response: (
                    ProfessionalB2BatchOutput | ProfessionalB2CorrectionOutput | None
                ) = None
                candidate_is_complete = False
                try:
                    previous_response_id = (
                        b2_response_id
                        if getattr(provider, "supports_response_continuation", False)
                        else None
                    )
                    is_group_correction = partial_beat_groups is not None
                    response_type = (
                        ProfessionalB2CorrectionOutput
                        if is_group_correction else ProfessionalB2BatchOutput
                    )
                    response = await _call_stage(
                        "B2", None, batch_input.model_dump(),
                        provider=provider, model=model, prompt="b2_professional.md",
                        instructions_suffix="\n\n" + feedback if feedback else "",
                        output_type=response_type,
                        input_image_url=image_url,
                        previous_response_id=previous_response_id,
                        correction_feedback=feedback if is_group_correction else None,
                        on_response_id=remember_b2_response,
                        on_input=on_input, on_api_response=on_api_response,
                        on_call_duration=on_call_duration,
                    )
                    raw_response = response
                    if is_group_correction and partial_base is not None:
                        candidate = merge_professional_b2_correction(
                            partial_base, response, prefix_source, partial_beat_groups,
                        )
                    else:
                        batch_parts = validate_professional_b2_batch(
                            response, b11.narrative_core, batch_blocks,
                        )
                        candidate = WholeProfessionalB2Output(
                            pipeline_stage="B2",
                            narrative_core=b11.narrative_core,
                            blocks=[
                                *(item.blocks[0] for item in accepted),
                                *(item.blocks[0] for item in batch_parts),
                            ],
                            error=None,
                        )
                    candidate_is_complete = bool(candidate.blocks)
                    accepted = validate_whole_professional_b2(candidate, prefix_source)
                    if on_output is not None:
                        for item in accepted:
                            on_output("B2", item.blocks[0].block_id, item.model_dump())
                    break
                except (
                    ValidationError, BPipelineValidationError, GridPlanError, RuntimeError
                ) as error:
                    if isinstance(error, RuntimeError) and not is_schema_failure(error):
                        raise
                    if (
                        raw_response is not None and raw_response.error is not None
                        and raw_response.error.code != "INVALID_UPSTREAM_INPUT"
                    ):
                        raise
                    if on_rejected_b2_output is not None:
                        on_rejected_b2_output(
                            0, api_attempt, str(error),
                            raw_response.model_dump() if raw_response is not None
                            else candidate.model_dump() if candidate is not None
                            else {"unparsed_response": True, "validation_error": str(error)},
                        )
                    if raw_response is not None and raw_response.error is not None:
                        if upstream_input_retries >= 1:
                            raise BPipelineValidationError(
                                f"B2 batch {batch_number} returned INVALID_UPSTREAM_INPUT "
                                "again after a Responses API correction: "
                                f"{raw_response.error.message}"
                            ) from error
                        upstream_input_retries += 1
                        partial_base = None
                        partial_beat_groups = None
                        feedback = _professional_b2_invalid_input_feedback(
                            raw_response.error, image_supplied=image_url is not None,
                            batch_number=batch_number, total_batches=total_batches,
                            block_ids=batch_ids,
                        )
                        continue
                    if validation_retries >= MAX_VALIDATION_RETRIES:
                        raise BPipelineValidationError(
                            f"B2 batch {batch_number} failed after "
                            f"{MAX_VALIDATION_RETRIES} validation retries: {error}"
                        ) from error
                    validation_retries += 1
                    failed_groups = _professional_b2_failed_beat_groups(error)
                    if (
                        failed_groups is not None
                        and candidate_is_complete
                        and candidate is not None
                    ):
                        partial_base = candidate.model_copy(deep=True)
                        partial_beat_groups = failed_groups
                        feedback = _professional_b2_partial_feedback(
                            error, partial_beat_groups, partial_base,
                        )
                    elif partial_beat_groups is not None and partial_base is not None:
                        feedback = _professional_b2_partial_feedback(
                            error,
                            partial_beat_groups,
                            partial_base,
                            correction_error=error,
                        )
                    else:
                        current_batch_response = (
                            raw_response
                            if isinstance(raw_response, ProfessionalB2BatchOutput)
                            else None
                        )
                        feedback = _professional_b2_batch_retry_feedback(
                            error,
                            batch_number=batch_number,
                            total_batches=total_batches,
                            block_ids=batch_ids,
                            previous=current_batch_response,
                        )
                except Exception as error:
                    status_code = getattr(error, "status_code", None)
                    if not isinstance(status_code, int) or status_code < 500:
                        raise
                    if on_rejected_b2_output is not None:
                        on_rejected_b2_output(
                            0, api_attempt, str(error),
                            {
                                "api_error": str(error),
                                "status_code": status_code,
                                "request_id": getattr(error, "request_id", None),
                            },
                        )
                    if server_error_retries >= 1:
                        raise BPipelineValidationError(
                            f"B2 batch {batch_number} Responses API returned HTTP "
                            f"{status_code} after its retry budget: {error}"
                        ) from error
                    server_error_retries += 1
                    retry_note = (
                        f"The previous Responses API request for batch {batch_number} "
                        f"ended with HTTP {status_code} before a structured result. "
                        "Retry only the current batch and preserve accepted prior batches."
                    )
                    feedback = f"{feedback}\n\n{retry_note}"
                    continue

        final_plan = WholeProfessionalB2Output(
            pipeline_stage="B2", narrative_core=b11.narrative_core,
            blocks=[item.blocks[0] for item in accepted], error=None,
        )
        if on_output is not None:
            on_output("B2", None, final_plan.model_dump())
        return accepted

    b2_started = perf_counter()
    try:
        if visual_style == "profesional":
            # A selective grid retry must preserve global visual coherence:
            # rerun the entire director, using the failed block as feedback.
            feedback = ""
            if retry_ids:
                feedback = (
                    "Correct downstream visual continuity in the complete video: "
                    + "; ".join(
                        f"block {key}: {value}"
                        for key, value in sorted((retry_b2_feedback or {}).items())
                    )
                )
                for target in retry_ids:
                    if b2_correction_counts.get(target, 0) >= MAX_VALIDATION_RETRIES:
                        raise BPipelineValidationError(
                            f"B2 block {target} exhausted its downstream retry budget"
                        )
                    b2_correction_counts[target] = b2_correction_counts.get(target, 0) + 1
            b2_results = await b2_whole_professional(feedback)
        else:
            b2_outcomes = await asyncio.gather(
                *(
                    b2_selected(meta, block, b12)
                    for meta, block, b12 in zip(b11.blocks, materialized, b12_results, strict=True)
                    if (not retry_ids or meta.block_id in retry_ids)
                    and (not previous_partial_b2 or meta.block_id not in previous_partial_b2)
                ),
                return_exceptions=True,
            )
            b2_failures = [item for item in b2_outcomes if isinstance(item, BaseException)]
            if b2_failures:
                # Finish every independent block before allowing a stage failure.
                raise b2_failures[0]
            generated_b2 = [
                item for item in b2_outcomes
                if isinstance(item, (B2Output, ProfessionalB2Output))
            ]
            if retry_ids:
                generated_by_id = {item.blocks[0].block_id: item for item in generated_b2}
                b2_results = [
                    generated_by_id.get(block_id, previous_by_id[block_id])
                    for block_id in range(1, len(materialized) + 1)
                ]
            else:
                generated_by_id = {item.blocks[0].block_id: item for item in generated_b2}
                b2_results = [
                    generated_by_id.get(block_id)
                    or (previous_partial_b2 or {}).get(block_id)
                    for block_id in range(1, len(materialized) + 1)
                ]
                if any(item is None for item in b2_results):
                    raise BPipelineValidationError("B2 partial recovery is missing a block")
                if previous_partial_b2:
                    for block_id, saved in previous_partial_b2.items():
                        if block_id < 1 or block_id > len(materialized):
                            raise BPipelineValidationError(
                                "B2 partial recovery has invalid block IDs"
                            )
                        meta, block, b12 = (
                            b11.blocks[block_id - 1], materialized[block_id - 1],
                            b12_results[block_id - 1],
                        )
                        expected = B2Input(
                            narrative_core=b11.narrative_core, current_block_id=block_id,
                            blocks=(B2InputBlock(
                                block_id=meta.block_id, type=meta.type,
                                emotional_entry=meta.emotional_entry,
                                emotional_exit=meta.emotional_exit, span=meta.span,
                                beats=validate_beats(b12, block),
                            ),),
                        )
                        validate_visuals(saved, expected)
                        if on_output is not None:
                            on_output("B2", block_id, saved.model_dump())


        # Parallel workers cannot see presenter runs crossing block boundaries.
        # Fix one eligible block at a time without re-running any other block.
        while (
            visual_style != "profesional"
            and (overflow := _overlong_avatar_run(b2_results)) is not None
        ):
            run, total = overflow
            offset = 0
            candidates: list[tuple[int, int, int, str]] = []
            for beat in run:
                left = offset
                right = total - left - beat.characters
                offset += beat.characters
                current_block = b2_results[beat.block_id - 1].blocks[0]
                index = next(
                    i for i, item in enumerate(current_block.beats)
                    if item.beat_id == beat.beat_id
                )
                mandatory = (
                    visual_style == "profesional"
                    and current_block.type == "close"
                    and index == len(current_block.beats) - 1
                )
                if not mandatory and (
                    b2_correction_counts.get(beat.block_id, 0) < MAX_VALIDATION_RETRIES
                ):
                    candidates.append((
                        max(left, right),
                        b2_correction_counts.get(beat.block_id, 0),
                        beat.block_id,
                        beat.beat_id,
                    ))
            if not candidates:
                raise BPipelineValidationError(
                    _avatar_run_error(run, total)
                    + "; no discretionary B2 block has validation retries remaining"
                )
            _, _, block_id, target_beat_id = min(candidates)
            previous = b2_results[block_id - 1]
            error = BPipelineValidationError(
                _avatar_run_error(
                    run, total, professional=visual_style == "profesional"
                )
            )
            if on_rejected_b2_output is not None:
                on_rejected_b2_output(
                    block_id, b2_call_numbers.get(block_id, 1),
                    str(error), previous.model_dump()
                )
            suffix = validation_feedback(
                "B2 " + visual_style, error, block_id=block_id,
                previous=previous.model_dump(),
                continued=(
                    bool(b2_last_response_ids.get(block_id))
                    and getattr(provider, "supports_response_continuation", False)
                ),
                instructions=(
                    (
                        "B2 whole-video audio-run correction: the joined professional "
                        "video has more than 120 consecutive audio=true narration "
                        "characters. "
                        f"Change visual assignment {target_beat_id} to a faithful "
                        "audio=false media_image or media_video beat, or break the "
                        "spoken run at another discretionary beat within THIS block. "
                        "audio=true is reserved for the narrator while preserving the "
                        "reference narration setup and the narrator's physical position "
                        "within that setup. Never change the mandatory professional "
                        "close anchor. Keep all frozen narration and upstream "
                        "fields unchanged."
                    )
                    if visual_style == "profesional"
                    else (
                        "B2 whole-video avatar-run correction: the joined video has "
                        "more than 120 consecutive avatar/avatar_media narration "
                        "characters. "
                        f"Change visual assignment {target_beat_id} to a faithful "
                        "non-presenter media_image or media_video visual, or break the "
                        "run at another discretionary beat within THIS block. "
                        "Keep all frozen narration and upstream fields unchanged."
                    )
                ),
            )
            index = block_id - 1
            b2_results[index] = await b2_one(
                b11.blocks[index], materialized[index], b12_results[index], suffix,
            )
        if visual_style == "profesional":
            _professional_grid_preflight(_professional_plan(b2_results))
        if visual_style == "casero" and stop_after == "B2.1":
            # B2.1 must see a valid immutable numbered grid assignment.
            # Repair invalid B2 numbering before B2.1 or any paid image work.
            while True:
                professional_results = [
                    item for item in b2_results
                    if isinstance(item, ProfessionalB2Output)
                ]
                if len(professional_results) != len(b2_results):
                    raise BPipelineValidationError(
                        "Professional B2.1 requires professional B2 outputs"
                    )
                try:
                    _professional_grid_preflight(_professional_plan(professional_results))
                    break
                except GridPlanError as error:
                    block_id = _grid_error_block_id(error)
                    if block_id is None:
                        raise BPipelineValidationError(
                            f"B2 grid preflight failed before B2.1: {error}"
                        ) from error
                    if b2_correction_counts.get(block_id, 0) >= MAX_VALIDATION_RETRIES:
                        raise BPipelineValidationError(
                            f"B2 block {block_id} exhausted retries before B2.1 grid preflight"
                        ) from error
                    previous = b2_results[block_id - 1]
                    correction = BPipelineValidationError(str(error))
                    if on_rejected_b2_output is not None:
                        on_rejected_b2_output(
                            block_id, b2_call_numbers.get(block_id, 1),
                            str(correction), previous.model_dump(),
                        )
                    suffix = validation_feedback(
                        "B2 profesional", correction, block_id=block_id,
                        previous=previous.model_dump(),
                        continued=(
                            bool(b2_last_response_ids.get(block_id))
                            and getattr(provider, "supports_response_continuation", False)
                        ),
                        instructions=(
                            "Repair only the failed B2 image-grid continuity numbering. "
                            "Keep every frozen beat and every valid visual decision. "
                            "B2.1 must receive a valid final grid plan."
                        ),
                    )
                    index = block_id - 1
                    b2_results[index] = await b2_one(
                        b11.blocks[index], materialized[index],
                        b12_results[index], suffix,
                    )
    finally:
        if on_stage_duration is not None:
            on_stage_duration("B2", perf_counter() - b2_started)

    cta_groups: tuple[tuple[str, ...], ...] = previous_cta_groups or ()
    cta_overrides: dict[str, ProfessionalCTAVisualBeat] = dict(
        previous_cta_overrides or {}
    )
    if cta_detection_task is not None:
        cta_groups = await cta_detection_task
    if cta_groups:
        if not avatar_image or not ebook_image:
            raise BPipelineValidationError("CTA images missing")

        async def direct_one(
            group_number: int, group: tuple[str, ...], feedback: str = "",
            previous_visuals: dict[str, ProfessionalCTAVisualBeat] | None = None,
        ) -> dict[str, ProfessionalCTAVisualBeat]:
            return await direct_professional_cta(
                group_number, group, cta_merged_b12,
                presenter=avatar_image, ebook=ebook_image,
                provider=provider, model=model, semaphore=semaphore,
                on_input=on_input, on_output=on_output,
                on_api_response=on_api_response,
                on_call_duration=on_call_duration, correction=feedback,
                previous_visuals=previous_visuals,
            )

        if not cta_overrides:
            directed = await asyncio.gather(*(
                direct_one(i, group) for i, group in enumerate(cta_groups, 1)
            ))
            for values in directed:
                cta_overrides.update(values)

        # The merged CTA and ordinary B2 must obey the global audio/grid
        # constraints before B2.1 or any paid image work.
        for attempt in range(MAX_VALIDATION_RETRIES + 1):
            audio_overflows: list[list[tuple[str, int]]] = []
            try:
                final_cta_plan = _professional_plan(
                    [x for x in b2_results if isinstance(x, ProfessionalB2Output)],
                    cta_groups=cta_groups, cta_overrides=cta_overrides,
                )
                audio_overflows = _overlong_audio_runs_in_plan(final_cta_plan)
                if audio_overflows:
                    descriptions = [
                        ", ".join(f"{beat_id} ({characters})" for beat_id, characters in run)
                        + f" total {sum(characters for _, characters in run)} characters"
                        for run in audio_overflows
                    ]
                    raise BPipelineValidationError(
                        "CTA merged audio=true runs exceed 120 characters: "
                        + " || ".join(descriptions)
                    )
                _professional_grid_preflight(final_cta_plan)
                break
            except (BPipelineValidationError, GridPlanError, ValueError) as exc:
                if attempt >= MAX_VALIDATION_RETRIES:
                    raise BPipelineValidationError(
                        f"CTA merged plan failed validation: {exc}"
                    ) from exc

                feedback_by_group: dict[int, str] = {}
                if audio_overflows:
                    for group_number, group in enumerate(cta_groups, 1):
                        group_set = set(group)
                        relevant = [
                            run for run in audio_overflows
                            if any(beat_id in group_set for beat_id, _ in run)
                        ]
                        if not relevant:
                            continue
                        lines = [
                            "VALIDATION FAILED: merged CTA presenter audio run",
                            "Correct only this CTA group; preserve its frozen beat text and IDs.",
                        ]
                        for run in relevant:
                            details = ", ".join(
                                f"{beat_id} ({characters} characters)"
                                for beat_id, characters in run
                            )
                            cta_members = [
                                beat_id for beat_id, _ in run if beat_id in group_set
                            ]
                            lines.append(
                                f"- Global run: {details}; total "
                                f"{sum(characters for _, characters in run)} characters "
                                f"(maximum {_MAX_CONSECUTIVE_AVATAR_CHARACTERS}). "
                                f"CTA beats in this run: {', '.join(cta_members)}."
                            )
                        lines.extend((
                            "Break every listed run by changing the minimum number of "
                            "discretionary CTA beats in this group from avatar/avatar_media "
                            "to a faithful media_image or media_video with audio=false. "
                            "Generated CTA B-roll must retain the ebook reference and "
                            "reference_sources=['ebook']; keep the beat text unchanged.",
                            "Preserve every valid previous choice below. Return the complete "
                            "CTA group, one entry per supplied beat, changing only what is "
                            "needed to satisfy the listed global runs.",
                            "Previous CTA decisions for this group:",
                            json.dumps(
                                [cta_overrides[beat_id].model_dump()
                                 for beat_id in group if beat_id in cta_overrides],
                                ensure_ascii=False, separators=(",", ":"),
                            ),
                        ))
                        feedback_by_group[group_number] = "\n".join(lines)
                else:
                    match = re.search(r"\b(\d+[A-Z]+)\b", str(exc))
                    target_ids = {match.group(1)} if match else set()
                    for group_number, group in enumerate(cta_groups, 1):
                        if not target_ids or target_ids.intersection(group):
                            feedback_by_group[group_number] = (
                                "VALIDATION FAILED: B2 CTA merged-plan correction\n"
                                f"- Failure: {exc}\n"
                                "Correct only this supplied CTA group. Preserve frozen narration "
                                "and all valid previous visuals. Return the complete group.\n"
                                "Previous CTA decisions: "
                                + json.dumps(
                                    [cta_overrides[beat_id].model_dump()
                                     for beat_id in group if beat_id in cta_overrides],
                                    ensure_ascii=False, separators=(",", ":"),
                                )
                            )
                if not feedback_by_group:
                    feedback_by_group = {
                        group_number: f"VALIDATION FAILED: {exc}. Correct this CTA group only."
                        for group_number in range(1, len(cta_groups) + 1)
                    }
                selected_numbers = set(feedback_by_group)
                repaired = await asyncio.gather(*(
                    direct_one(
                        i, group, feedback_by_group[i],
                        previous_visuals={
                            beat_id: cta_overrides[beat_id]
                            for beat_id in group if beat_id in cta_overrides
                        },
                    )
                    for i, group in enumerate(cta_groups, 1)
                    if i in selected_numbers
                ))
                updated_overrides = dict(cta_overrides)
                for values in repaired:
                    updated_overrides.update(values)
                cta_overrides = updated_overrides

    result = BPipelineResult(
        b11=b11,
        b12=tuple(b12_results),
        b2=tuple(b2_results),
        cta_groups=cta_groups, cta_overrides=cta_overrides,
        visual_style=visual_style,
        b2_correction_counts=b2_correction_counts,
        b2_response_ids=b2_last_response_ids,
    )
    if on_stage_complete is not None:
        on_stage_complete("B2", len(b2_results), result.merged_b2_output())
    if stop_after == "B2":
        return result

    professional_results = tuple(
        item for item in b2_results if isinstance(item, ProfessionalB2Output)
    )
    if len(professional_results) != len(b2_results):
        raise BPipelineValidationError("B2.1 requires professional B2 outputs")

    source_plan = _professional_plan(
        professional_results, cta_groups=cta_groups, cta_overrides=cta_overrides,
    )
    split_candidates = _b21_split_candidates(source_plan)
    b21_payload = result.merged_b2_output()
    for block in b21_payload["blocks"]:
        for beat in block["beats"]:
            beat.pop("cta_group_id", None)
            beat.pop("cta_reference_sources", None)
    b21_payload["b21_review"] = {
        "protected_cta_beat_ids": [
            beat_id for group in cta_groups for beat_id in group
        ],
        "split_candidates": [
            {"block_id": item["block_id"], "beat_id": item["beat_id"]}
            for item in split_candidates
        ],
        "required_images_per_candidate": 2,
    }

    b21_started = perf_counter()
    try:
        feedback = ""
        b21: B21Output | None = None
        for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
            candidate: B21Output | None = None
            try:
                async with semaphore:
                    candidate = await _call_stage(
                        "B2.1",
                        None,
                        b21_payload,
                        provider=provider,
                        model=model,
                        prompt="b2_1_professional.md",
                        instructions_suffix="\n\n" + feedback if feedback else "",
                        output_type=B21Output,
                        input_image_url=image_url,
                        on_input=on_input,
                        on_api_response=on_api_response,
                        on_call_duration=on_call_duration,
                    )
                # CTA decisions are immutable: discard B2.1 attempts to
                # patch those beat IDs BEFORE any structural validation.
                protected = {id for group in cta_groups for id in group}
                if candidate.modified_beats is not None:
                    candidate = candidate.model_copy(update={
                        "modified_beats": [
                            patch for patch in candidate.modified_beats
                            if patch.beat_id not in protected
                        ]
                    })
                validate_b21(
                    candidate,
                    source_plan=source_plan,
                    split_candidates=split_candidates,
                )
                # Preflight the exact reviewed plan before paid images.
                reviewed_plan = apply_b21_review(source_plan, candidate)
                _professional_grid_preflight(reviewed_plan)
                b21 = candidate
                if on_output is not None:
                    on_output("B2.1", None, candidate.model_dump())
                break
            except (
                ValidationError,
                BPipelineValidationError,
                GridPlanError,
                RuntimeError,
            ) as error:
                if isinstance(error, RuntimeError) and not is_schema_failure(error):
                    raise
                rejected = candidate.model_dump() if candidate is not None else {
                    "unparsed_response": True,
                    "validation_error": str(error),
                }
                if on_rejected_b21_output is not None:
                    on_rejected_b21_output(attempt, str(error), rejected)
                if attempt > MAX_VALIDATION_RETRIES:
                    raise BPipelineValidationError(
                        f"B2.1 failed validation after {MAX_VALIDATION_RETRIES} "
                        f"retries: {error}"
                    ) from error
                feedback = validation_feedback(
                    "B2.1 profesional",
                    error,
                    previous=candidate.model_dump() if candidate else None,
                    instructions=(
                        "Repair only the sparse B2.1 visual patch. Never change frozen "
                        "B2 structure, IDs, narration, visual_type, reference or audio. "
                        "Every deterministic split candidate must have exactly two "
                        "non-identical still prompts. A numbered source must remain in "
                        "grid mode. Non-candidates cannot gain a second image. "
                        "Keep unrelated valid patches unchanged."
                    ),
                )
        if b21 is None:
            raise BPipelineValidationError("B2.1 did not produce a validated output")
    finally:
        if on_stage_duration is not None:
            on_stage_duration("B2.1", perf_counter() - b21_started)

    result = BPipelineResult(
        b11=b11,
        b12=tuple(b12_results),
        b2=tuple(b2_results),
        b21=b21,
        cta_groups=cta_groups, cta_overrides=cta_overrides,
        visual_style=visual_style,
        b2_correction_counts=b2_correction_counts,
        b2_response_ids=b2_last_response_ids,
    )
    if on_stage_complete is not None:
        on_stage_complete("B2.1", 1, result.merged_b21_output())
    if stop_after == "B2.1":
        return result

