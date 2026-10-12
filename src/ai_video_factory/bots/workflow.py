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
from typing import Literal

from pydantic import BaseModel, ValidationError

from ai_video_factory.config import settings
from ai_video_factory.providers.base import StructuredTextProvider
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
    B12Input,
    B12Output,
    B21Output,
    Beat,
    BlockMeta,
    MaterializedBlock,
    ProfessionalB2Output,
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
    """Normalize layout and punctuation while preserving words and their order."""
    return "".join(
        character
        for character in text
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


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
        first, last = block.span.first_words, block.span.last_words
        if first[0].isspace() or last[-1].isspace():
            raise BPipelineValidationError("B1.1 anchors must begin/end at substantive text")
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
        if not _without_whitespace_and_punctuation(beat.text) or len(beat.text.split()) > 40:
            raise BPipelineValidationError(f"Invalid B1.2 beat text or length: {beat.beat_id}")
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
    length_errors = _beat_character_length_errors(result.beats)
    if length_errors:
        raise BPipelineValidationError("; ".join(length_errors))
    return result.beats


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


@dataclass(frozen=True)
class _AvatarRunBeat:
    block_id: int
    beat_id: str
    characters: int


def _overlong_avatar_run(
    outputs: list[B2Output | ProfessionalB2Output] | tuple[B2Output | ProfessionalB2Output, ...],
) -> tuple[list[_AvatarRunBeat], int] | None:
    """Find the first overlong spoken run in whole-video beat order.

    Professional B2 defines a run by consecutive audio=true beats, regardless of
    visual_type. Casero has no audio flag, so it retains the historical
    avatar/avatar_media presenter-run rule.
    """
    run: list[_AvatarRunBeat] = []
    characters = 0
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
                    return run, characters
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
        return run, characters
    return None


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

    B2 owns visual decisions only. If the model changes the text of an
    otherwise structurally matching beat (including CRLF/LF normalization),
    the frozen B1.2 text is authoritative and is restored locally.
    Structural changes such as changed beat IDs or beat counts remain errors.
    """
    if result.error is not None or not result.blocks:
        return result
    actual = result.blocks[0]
    expected = supplied.blocks[0]
    if (
        len(actual.beats) != len(expected.beats)
        or [beat.beat_id for beat in actual.beats]
        != [beat.beat_id for beat in expected.beats]
    ):
        return result
    repaired = result.model_copy(deep=True)
    for visual, upstream in zip(repaired.blocks[0].beats, expected.beats, strict=True):
        visual.text = upstream.text
    return repaired


def validate_visuals(result: B2Output | ProfessionalB2Output, supplied: B2Input) -> None:
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
    overflow = _overlong_avatar_run((result,))
    if overflow is not None:
        raise BPipelineValidationError(
            _avatar_run_error(
                *overflow, professional=isinstance(result, ProfessionalB2Output)
            )
        )


_B21_IMAGE_SPLIT_THRESHOLD = 70
_B21_NUMBERED_PROMPT = re.compile(r"^\s*([1-9]|1[0-8])\.\s+(.+)$", re.DOTALL)


def _b21_prompt_body(prompt: str) -> str:
    matched = _B21_NUMBERED_PROMPT.match(prompt)
    return matched.group(2).strip() if matched is not None else prompt.strip()


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
    """Return valid source-numbered B2 runs without changing their prompts."""
    runs: list[list[str]] = []
    current: list[str] = []
    for beat in block["beats"]:
        prompt = beat.get("prompt")
        matched = (
            _B21_NUMBERED_PROMPT.match(prompt)
            if isinstance(prompt, str) else None
        )
        if matched is None:
            if current:
                runs.append(current)
                current = []
            continue
        number = int(matched.group(1))
        if number == 1:
            if current:
                runs.append(current)
            current = [beat["beat_id"]]
        elif current and number == len(current) + 1:
            current.append(beat["beat_id"])
        else:
            # Raw B2 grid preflight owns the detailed error. Treat malformed
            # numbering as a boundary here so B2.1 never tries to repair it.
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    return [run for run in runs if len(run) >= 2]


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
    """Pack logical continuity runs of up to 18 assets, preserving beat subshot pairs."""
    chunks: list[list[tuple[dict[str, object], list[str]]]] = []
    current: list[tuple[dict[str, object], list[str]]] = []
    count = 0
    for unit in units:
        size = len(unit[1])
        if current and count + size > 18:
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
    source_runs = {
        block["block_id"]: [
            run for run in _b21_numbered_runs(block)
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
        for block_id, runs in source_runs.items()
        for run in runs for beat_id in run
    }

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
                    first, second = f"1. {first}", f"2. {second}"
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

    # Existing B2 numbered continuity is authoritative. Insert any new subshot
    # immediately after its source image, then shift/restart numbering in code.
    reviewed_by_block = {
        block["block_id"]: block for block in reviewed["blocks"]
    }
    for block_id, runs in source_runs.items():
        block = reviewed_by_block[block_id]
        beats = {beat["beat_id"]: beat for beat in block["beats"]}
        for run in runs:
            units: list[tuple[dict[str, object], list[str]]] = []
            for beat_id in run:
                beat = beats[beat_id]
                subshots = beat.get("visual_subshots")
                if isinstance(subshots, list):
                    prompts = [
                        _b21_prompt_body(str(item["prompt"])) for item in subshots
                    ]
                else:
                    prompts = [_b21_prompt_body(str(beat["prompt"]))]
                units.append((beat, prompts))
            for chunk in _b21_pack_grid_units(units):
                number = 1
                for beat, prompts in chunk:
                    subshots = beat.get("visual_subshots")
                    if isinstance(subshots, list):
                        for item, body in zip(subshots, prompts, strict=True):
                            item["generation"] = "grid"
                            item["prompt"] = f"{number}. {body}"
                            number += 1
                        beat["prompt"] = subshots[0]["prompt"]
                    else:
                        beat["prompt"] = f"{number}. {prompts[0]}"
                        number += 1
    return reviewed


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
        submitted = dict(payload)
        if correction_feedback:
            submitted = {
                **submitted,
                "_validation_correction": correction_feedback,
                "_previous_response_id": previous_response_id if continued else None,
            }
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
    """Validate the global response, then recover immutable per-block checkpoints."""
    _require_success("B1.2", result.error)
    if result.narrative_core != core or not result.blocks:
        raise BPipelineValidationError("B1.2 altered the frozen narrative core")
    if len(result.blocks) != len(materialized):
        raise BPipelineValidationError("B1.2 changed the frozen block count")
    outputs: list[B12Output] = []
    for actual, block in zip(result.blocks, materialized, strict=True):
        if actual.block_id != block.block_id:
            raise BPipelineValidationError("B1.2 altered block IDs or order")
        part = B12Output(
            pipeline_stage="B1.2", block_id=block.block_id,
            beats=actual.beats, error=None,
        )
        validate_beats(part, block)
        if "".join(beat.text for beat in actual.beats) != block.text:
            raise BPipelineValidationError(
                f"B1.2 block {block.block_id} does not reconstruct source exactly"
            )
        outputs.append(part)
    if "".join(block.text for block in materialized) != script:
        raise BPipelineValidationError("Whole B1.2 input blocks do not reconstruct script")
    return outputs


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
    if "".join(beat.text for block in source.blocks for beat in block.beats) != source.plain_script_for_recording:
        raise BPipelineValidationError("B2 source beats do not reconstruct full script")
    outputs: list[ProfessionalB2Output] = []
    for actual, expected in zip(result.blocks, source.blocks, strict=True):
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
        validate_visuals(part, supplied)
        outputs.append(part)
    overflow = _overlong_avatar_run(outputs)
    if overflow is not None:
        raise BPipelineValidationError(_avatar_run_error(*overflow, professional=True))
    _professional_grid_preflight(_professional_plan(outputs))
    return outputs


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
            for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
                candidate: WholeB12Output | None = None
                try:
                    candidate = await _call_stage(
                        "B1.2", None, b12_input.model_dump(),
                        provider=provider, model=model, prompt="b1_2.md",
                        instructions_suffix="\n\n" + feedback if feedback else "",
                        correction_feedback=feedback or None,
                        previous_response_id=previous_response_id,
                        on_response_id=remember_b12,
                        output_type=WholeB12Output,
                        on_input=on_input,
                        on_api_response=on_api_response,
                        on_call_duration=on_call_duration,
                    )
                    b12_results = validate_whole_beats(
                        candidate, b11.narrative_core, materialized, script,
                    )
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
                    if attempt > MAX_VALIDATION_RETRIES:
                        raise BPipelineValidationError(
                            f"Whole-video B1.2 failed after {MAX_VALIDATION_RETRIES} retries: {error}"
                        ) from error
                    previous_response_id = (
                        b12_response_id if getattr(provider, "supports_response_continuation", False)
                        else None
                    )
                    feedback = validation_feedback(
                        "B1.2 whole video", error,
                        previous=candidate.model_dump() if candidate else None,
                        continued=previous_response_id is not None,
                        instructions=(
                            "Correct the COMPLETE whole-video beat segmentation. "
                            "Preserve every B1.1 block, ID, and source character. "
                            "Every beat must be a contiguous verbatim slice including "
                            "punctuation and whitespace, max 40 words and 100 stripped "
                            "characters, with no minimum length. Preserve global rhythm."
                        ),
                    )
            if on_output is not None:
                on_output("B1.2", None, candidate.model_dump())
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
        # One director chooses the carrier for every pulse in full-video context.
        # Keep existing per-block outputs for B2.1, CTA merge, TTS and checkpoints.
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
        request = WholeProfessionalB2Input(
            plain_script_for_recording=script, narrative_core=b11.narrative_core,
            blocks=frozen_blocks,
            cta_groups=[list(group) for group in reserved_groups]
            if reserved_groups is not None else None,
        )
        feedback = downstream_feedback
        for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
            candidate: WholeProfessionalB2Output | None = None
            try:
                # Each complete retry is sent with the original presenter image.
                candidate = await _call_stage(
                    "B2", None, request.model_dump(),
                    provider=provider, model=model, prompt="b2_professional.md",
                    instructions_suffix="\n\n" + feedback if feedback else "",
                    output_type=WholeProfessionalB2Output,
                    input_image_url=image_url,
                    on_input=on_input, on_api_response=on_api_response,
                    on_call_duration=on_call_duration,
                )
                parts = validate_whole_professional_b2(candidate, request)
                if on_output is not None:
                    on_output("B2", None, candidate.model_dump())
                    for item in parts:
                        on_output("B2", item.blocks[0].block_id, item.model_dump())
                return parts
            except (ValidationError, BPipelineValidationError, GridPlanError, RuntimeError) as error:
                if isinstance(error, RuntimeError) and not is_schema_failure(error):
                    raise
                if candidate is not None and candidate.error is not None:
                    raise
                if on_rejected_b2_output is not None:
                    on_rejected_b2_output(
                        0, attempt, str(error),
                        candidate.model_dump() if candidate is not None
                        else {"unparsed_response": True, "validation_error": str(error)},
                    )
                if attempt > MAX_VALIDATION_RETRIES:
                    raise BPipelineValidationError(
                        f"Whole professional B2 failed after {MAX_VALIDATION_RETRIES} retries: {error}"
                    ) from error
                feedback = validation_feedback(
                    "B2 profesional whole video", error,
                    previous=candidate.model_dump() if candidate else None,
                    continued=False,
                    instructions=(
                        "Correct the COMPLETE visual plan. Preserve all source text, "
                        "blocks, beat IDs, and beat types. Keep valid carrier choices "
                        "and correct only invalid visual/audio/reference assignments "
                        "or prompts. Enforce the GLOBAL 120-character audio=true cap, "
                        "the final close avatar, and valid block-local grid numbering."
                    ),
                )

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
                            raise BPipelineValidationError("B2 partial recovery has invalid block IDs")
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
        while visual_style != "profesional" and (overflow := _overlong_avatar_run(b2_results)) is not None:
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
                    match = re.search(r"(?:at |description at )(\d+)[A-Z]+", str(error))
                    if match is None:
                        raise BPipelineValidationError(
                            f"B2 grid preflight failed before B2.1: {error}"
                        ) from error
                    block_id = int(match.group(1))
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
        ) -> dict[str, ProfessionalCTAVisualBeat]:
            return await direct_professional_cta(
                group_number, group, cta_merged_b12,
                presenter=avatar_image, ebook=ebook_image,
                provider=provider, model=model, semaphore=semaphore,
                on_input=on_input, on_output=on_output,
                on_api_response=on_api_response,
                on_call_duration=on_call_duration, correction=feedback,
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
            try:
                final_cta_plan = _professional_plan(
                    [x for x in b2_results if isinstance(x, ProfessionalB2Output)],
                    cta_groups=cta_groups, cta_overrides=cta_overrides,
                )
                spoken_run = 0
                for block in final_cta_plan["blocks"]:
                    for beat in block["beats"]:
                        if beat["audio"]:
                            spoken_run += len(beat["text"].strip())
                            if spoken_run > _MAX_CONSECUTIVE_AVATAR_CHARACTERS:
                                raise BPipelineValidationError(
                                    "CTA merged audio=true run exceeds 120 characters"
                                )
                        else:
                            spoken_run = 0
                _professional_grid_preflight(final_cta_plan)
                break
            except (BPipelineValidationError, GridPlanError, ValueError) as exc:
                if attempt >= MAX_VALIDATION_RETRIES:
                    raise BPipelineValidationError(
                        f"CTA merged plan failed validation: {exc}"
                    ) from exc
                # B2 is frozen. Only B2 CTA may correct the invalid merge.
                repaired = await asyncio.gather(*(
                    direct_one(i, group, str(exc))
                    for i, group in enumerate(cta_groups, 1)
                ))
                cta_overrides = {
                    k: v for values in repaired for k, v in values.items()
                }

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

