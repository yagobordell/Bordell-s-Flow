"""B1.1 → independent B1.2/B2 per-block workers, with exact source integrity.

This module preserves the new string beat IDs and four visual formats exactly.
Downstream GPU clients must adopt these contracts before an end-to-end video
runner can be enabled; no lossy integer-shot conversion is performed.
"""

import asyncio
import base64
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ValidationError

from ai_video_factory.config import settings
from ai_video_factory.providers.base import StructuredTextProvider

from .contracts import (
    B2Input,
    B2InputBlock,
    B2Output,
    B11Output,
    B12Input,
    B12Output,
    Beat,
    BlockMeta,
    MaterializedBlock,
    ProfessionalB2Output,
)

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
    if _without_whitespace_and_punctuation(actual_text) != _without_whitespace_and_punctuation(
        block.text
    ):
        raise BPipelineValidationError(
            "B1.2 beat text differs from the materialized block beyond whitespace or punctuation"
        )
    length_errors = _beat_character_length_errors(result.beats, block)
    if length_errors:
        raise BPipelineValidationError("; ".join(length_errors))
    return result.beats


def _beat_character_length_errors(
    beats: list[Beat], block: MaterializedBlock
) -> list[str]:
    """Count decoded narration characters, excluding boundary whitespace."""
    errors: list[str] = []
    only_short_block = len(beats) == 1 and len(block.text.strip()) <= 12
    for beat in beats:
        characters = len(beat.text.strip())
        if characters > 100:
            errors.append(
                f"B1.2 beat {beat.beat_id} has {characters} characters (maximum 100); "
                "split it into two or more beats"
            )
        elif characters <= 12 and not only_short_block:
            errors.append(
                f"B1.2 beat {beat.beat_id} has {characters} characters (minimum 13); "
                "merge it with the previous or next beat in the same block"
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
    """Find the first overlong presenter run in whole-video beat order."""
    run: list[_AvatarRunBeat] = []
    characters = 0
    for output in outputs:
        if not output.blocks:
            raise BPipelineValidationError("B2 output is missing its block")
        block = output.blocks[0]
        for beat in block.beats:
            if beat.visual_type not in _PRESENTER_VISUAL_TYPES:
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


def _avatar_run_error(run: list[_AvatarRunBeat], characters: int) -> str:
    beats = ", ".join(f"{beat.beat_id} ({beat.characters})" for beat in run)
    return (
        f"B2 consecutive avatar/avatar_media beats {beats} total {characters} characters "
        f"(maximum {_MAX_CONSECUTIVE_AVATAR_CHARACTERS}); assign a faithful non-presenter "
        "visual to a discretionary beat without changing frozen narration or mandatory anchors"
    )


def validate_visuals(result: B2Output | ProfessionalB2Output, supplied: B2Input) -> None:
    _require_success("B2", result.error)
    if result.narrative_core != supplied.narrative_core or not result.blocks:
        raise BPipelineValidationError("B2 altered the narrative core or omitted the block")
    actual = result.blocks[0]
    expected = supplied.blocks[0]
    if actual.model_dump(exclude={"beats"}) != expected.model_dump(exclude={"beats"}):
        raise BPipelineValidationError("B2 altered frozen B1.1 block metadata")
    if len(actual.beats) != len(expected.beats):
        raise BPipelineValidationError("B2 changed the number of beats")
    for visual, upstream in zip(actual.beats, expected.beats, strict=True):
        restored = visual.model_dump(exclude={
            "visual_type", "description", "prompt", "video_prompt", "reference", "audio"
        })
        if restored != upstream.model_dump():
            raise BPipelineValidationError("B2 changed a frozen B1.2 beat")
    overflow = _overlong_avatar_run((result,))
    if overflow is not None:
        raise BPipelineValidationError(_avatar_run_error(*overflow))


@dataclass(frozen=True)
class BPipelineResult:
    b11: B11Output
    b12: tuple[B12Output, ...]
    b2: tuple[B2Output | ProfessionalB2Output, ...]
    visual_style: Literal["casero", "profesional"] = "casero"


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
            "blocks": [item.blocks[0].model_dump() for item in self.b2 if item.blocks],
        }

    def visual_plan(self) -> dict[str, object]:
        """Application-owned ordered join, not a bot-owned B2 output object."""
        if self.b11.narrative_core is None:
            raise BPipelineValidationError("B1.1 narrative core is missing")
        return {
            "schema_version": "b-pipeline-v1",
            **({"visual_style": self.visual_style} if self.visual_style == "profesional" else {}),
            "narrative_core": self.b11.narrative_core.model_dump(),
            "blocks": [result.blocks[0].model_dump() for result in self.b2 if result.blocks],
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
) -> OutputT:
    """Record exactly the submitted input and meter the returned API response."""
    if on_input is not None:
        on_input(stage, block_id, payload)
    kwargs = {
        "model": model,
        "instructions": _instructions(prompt) + instructions_suffix,
        "input_text": _payload(payload),
        "output_type": output_type,
    }
    if input_image_url is not None:
        kwargs["input_image_url"] = input_image_url
    started = perf_counter()
    try:
        metered = getattr(provider, "generate_structured_with_response", None)
        if callable(metered):
            output, response = await metered(**kwargs)
            if on_api_response is not None:
                on_api_response(stage, block_id, response)
            return output
        return await provider.generate_structured(**kwargs)
    finally:
        if on_call_duration is not None:
            on_call_duration(stage, block_id, perf_counter() - started)


async def run_b_pipeline(
    script: str,
    *,
    provider: StructuredTextProvider,
    model: str | None = None,
    max_parallel_calls: int = 8,
    visual_style: Literal["casero", "profesional"] = "casero",
    avatar_image: bytes | None = None,
    start_from: Literal["B1.1", "B1.2", "B2"] = "B1.1",
    stop_after: Literal["B1.1", "B1.2", "B2"] = "B2",
    previous_b11: B11Output | None = None,
    previous_b12: tuple[B12Output, ...] | None = None,
    previous_b2: tuple[B2Output, ...] | None = None,
    retry_b2_block_ids: frozenset[int] | None = None,
    on_input: Callable[[str, int | None, dict[str, object]], None] | None = None,
    on_output: Callable[[str, int | None, dict[str, object]], None] | None = None,
    on_rejected_output: Callable[[int, str, dict[str, object]], None] | None = None,
    on_rejected_b12_output: (
        Callable[[int, int, str, dict[str, object]], None] | None
    ) = None,
    on_rejected_b2_output: (
        Callable[[int, int, str, dict[str, object]], None] | None
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
    if stop_after == "B2":
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
    if stop_after not in {"B1.1", "B1.2", "B2"}:
        raise ValueError(f"Unknown stop stage: {stop_after}")
    if ("B1.1", "B1.2", "B2").index(stop_after) < ("B1.1", "B1.2", "B2").index(start_from):
        raise ValueError("--stop cannot be earlier than --from")

    async def call_b11(instructions_suffix: str = "") -> B11Output:
        output = await _call_stage(
            "B1.1",
            None,
            {"plain_script_for_recording": script},
            provider=provider,
            model=model,
            prompt="b1_1.md",
            instructions_suffix=instructions_suffix,
            output_type=B11Output,
            on_input=on_input,
            on_api_response=on_api_response,
            on_call_duration=on_call_duration,
        )
        _require_success("B1.1", output.error)
        if output.narrative_core is None or output.blocks is None:
            raise BPipelineValidationError(
                "B1.1 did not provide required successful fields"
            )
        return output

    if start_from == "B1.1":
        b11_started = perf_counter()
        try:
            b11 = await call_b11()
            try:
                materialized = materialize_blocks(script, b11.blocks)
            except BPipelineValidationError as first_error:
                if on_rejected_output is not None:
                    on_rejected_output(1, str(first_error), b11.model_dump())
                retry_suffix = (
                    "\n\nB1.1 automatic correction attempt. The previous response failed "
                    f"deterministic source-anchor validation: {first_error}\n"
                    "Return a corrected complete B1.1 object. Preserve the previous block "
                    "architecture and narrative metadata; correct the anchors so each is "
                    "copied verbatim from the script and identifies the intended boundary "
                    "uniquely. Make an anchor longer when a phrase occurs more than once. "
                    "The ordered blocks must cover the complete script exactly once.\n"
                    "Previous B1.1 response:\n"
                    f"{_payload(b11.model_dump())}"
                )
                retry_b11: B11Output | None = None
                try:
                    retry_b11 = await call_b11(retry_suffix)
                    materialized = materialize_blocks(script, retry_b11.blocks)
                    b11 = retry_b11
                except BPipelineValidationError as retry_error:
                    if on_rejected_output is not None and retry_b11 is not None:
                        on_rejected_output(2, str(retry_error), retry_b11.model_dump())
                    raise BPipelineValidationError(
                        "B1.1 source-anchor validation failed after one automatic retry: "
                        f"{retry_error}"
                    ) from retry_error
            if on_output is not None:
                on_output("B1.1", None, b11.model_dump())
        finally:
            if on_stage_duration is not None:
                on_stage_duration("B1.1", perf_counter() - b11_started)
        if on_stage_complete is not None:
            on_stage_complete("B1.1", len(materialized), None)
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
        # Two independent parallel waves: no B2 starts until every B1.2 succeeds.
        async def b12_one(block: MaterializedBlock) -> B12Output:
            b12_input = B12Input(
                narrative_core=b11.narrative_core,
                current_block_id=block.block_id,
                blocks=(block,),
            )
            async def call_b12(instructions_suffix: str = "") -> B12Output:
                async with semaphore:
                    return await _call_stage(
                        "B1.2",
                        block.block_id,
                        b12_input.model_dump(),
                        provider=provider,
                        model=model,
                        prompt="b1_2.md",
                        instructions_suffix=instructions_suffix,
                        output_type=B12Output,
                        on_input=on_input,
                        on_api_response=on_api_response,
                        on_call_duration=on_call_duration,
                    )

            output = await call_b12()
            try:
                validate_beats(output, block)
            except BPipelineValidationError as first_error:
                # Only the new character-boundary defects trigger a paid correction.
                # Other upstream failures retain B1.2's existing fail-closed behavior.
                if not output.beats or not _beat_character_length_errors(output.beats, block):
                    raise
                if on_rejected_b12_output is not None:
                    on_rejected_b12_output(
                        block.block_id, 1, str(first_error), output.model_dump()
                    )
                retry_suffix = (
                    "\n\nB1.2 automatic beat-length correction. Your previous response "
                    f"failed validation: {first_error}\n"
                    "Return a corrected complete B1.2 object for the SAME original input "
                    "block. Every beat must contain 13 to 100 characters after stripping "
                    "boundary whitespace, except when the entire block is one beat with "
                    "at most 12 characters. Split beats longer than 100 characters into "
                    "as many contiguous source slices as necessary. Merge beats of at "
                    "most 12 characters into the preceding or following beat within "
                    "this block; choose the semantically appropriate neighbor without "
                    "exceeding 100 characters or 40 words. If needed, relocate adjacent "
                    "boundaries. Never edit, remove, reorder or duplicate source characters: "
                    "the concatenated beat texts MUST equal blocks[0].text character for "
                    "character. Reassign beat IDs consecutively from the block ID plus A, "
                    "and assign beat_type for each resulting beat. Keep the original block "
                    "metadata and narrative context unchanged. Follow the output schema.\n"
                    "Previous invalid B1.2 response:\n"
                    f"{_payload(output.model_dump())}"
                )
                retry_output = await call_b12(retry_suffix)
                try:
                    validate_beats(retry_output, block)
                    if "".join(beat.text for beat in retry_output.beats or []) != block.text:
                        raise BPipelineValidationError(
                            "B1.2 corrected beats do not reconstruct the original block exactly"
                        )
                except BPipelineValidationError as retry_error:
                    if on_rejected_b12_output is not None:
                        on_rejected_b12_output(
                            block.block_id, 2, str(retry_error), retry_output.model_dump()
                        )
                    raise BPipelineValidationError(
                        f"B1.2 block {block.block_id} failed character-length correction "
                        f"after one retry: {retry_error}"
                    ) from retry_error
                output = retry_output
            if on_output is not None:
                on_output("B1.2", block.block_id, output.model_dump())
            return output

        b12_started = perf_counter()
        try:
            b12_results = await asyncio.gather(*(b12_one(block) for block in materialized))
        finally:
            if on_stage_duration is not None:
                on_stage_duration("B1.2", perf_counter() - b12_started)
        if on_stage_complete is not None:
            partial = BPipelineResult(b11=b11, b12=tuple(b12_results), b2=())
            on_stage_complete("B1.2", len(b12_results), partial.merged_b12_output())
    if stop_after == "B1.2":
        return BPipelineResult(b11=b11, b12=tuple(b12_results), b2=())

    async def b2_one(
        meta: BlockMeta, block: MaterializedBlock, b12: B12Output,
        correction_suffix: str = "",
        rejection_attempt_offset: int = 0,
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
        async def call_b2(instructions_suffix: str = "") -> B2Output | ProfessionalB2Output:
            async with semaphore:
                return await _call_stage(
                    "B2",
                    block.block_id,
                    b2_input.model_dump(),
                    provider=provider,
                    model=model,
                    prompt="b2_professional.md" if visual_style == "profesional" else "b2.md",
                    instructions_suffix=instructions_suffix,
                    output_type=ProfessionalB2Output if visual_style == "profesional" else B2Output,
                    input_image_url=image_url,
                    on_input=on_input,
                    on_api_response=on_api_response,
                    on_call_duration=on_call_duration,
                )

        def schema_error_payload(error: ValidationError) -> dict[str, object]:
            return {
                "unparsed_response": True,
                "schema_errors": json.loads(error.json(include_url=False)),
            }

        async def retry_after_schema_error(
            first_error: ValidationError,
        ) -> B2Output | ProfessionalB2Output:
            if on_rejected_b2_output is not None:
                on_rejected_b2_output(
                    block.block_id,
                    rejection_attempt_offset + 1,
                    str(first_error),
                    schema_error_payload(first_error),
                )
            visual_rules = (
                "For professional B2, prompt=null is permitted only for a reused "
                "avatar with reference=false; all other visuals require a "
                "nonempty prompt. media_video requires a nonempty video_prompt; "
                "all other visual types require video_prompt=null. "
                "avatar/avatar_media require audio=true; "
                "media_image ALWAYS requires audio=false. Generated "
                "synchronized visible speech must use media_video with audio=true. "
                "Do not omit reference or audio booleans."
                if visual_style == "profesional"
                else (
                    "visual_type='avatar' requires description=null; every other "
                    "visual_type requires a nonempty description."
                )
            )
            retry_suffix = (
                "\n\nB2 automatic schema correction attempt. Your previous structured "
                "response could not be parsed because it violated the required output "
                f"schema: {first_error}\n"
                "Return one complete corrected B2 object for the same input block. "
                "Preserve the narrative core and every upstream block and beat field "
                "exactly. Follow the supplied output schema. "
                f"{visual_rules} Do not omit required fields."
            )
            try:
                retry_output = await call_b2(correction_suffix + retry_suffix)
            except ValidationError as retry_error:
                if on_rejected_b2_output is not None:
                    on_rejected_b2_output(
                        block.block_id,
                        rejection_attempt_offset + 2,
                        str(retry_error),
                        schema_error_payload(retry_error),
                    )
                raise BPipelineValidationError(
                    f"B2 block {block.block_id} failed schema validation after one retry: "
                    f"{retry_error}"
                ) from retry_error
            try:
                validate_visuals(retry_output, b2_input)
            except BPipelineValidationError as retry_error:
                if on_rejected_b2_output is not None:
                    on_rejected_b2_output(
                        block.block_id,
                        rejection_attempt_offset + 2,
                        str(retry_error),
                        retry_output.model_dump(),
                    )
                raise BPipelineValidationError(
                    f"B2 block {block.block_id} failed validation after schema retry: "
                    f"{retry_error}"
                ) from retry_error
            return retry_output

        try:
            output = await call_b2(correction_suffix)
        except ValidationError as first_schema_error:
            output = await retry_after_schema_error(first_schema_error)
        else:
            try:
                validate_visuals(output, b2_input)
            except BPipelineValidationError as first_error:
                if on_rejected_b2_output is not None:
                    on_rejected_b2_output(
                        block.block_id, rejection_attempt_offset + 1,
                        str(first_error), output.model_dump()
                    )
                retry_suffix = (
                    "\n\nB2 automatic correction attempt. Your previous structured response "
                    f"failed output validation: {first_error}\n"
                    "Return a corrected complete B2 object for the same input block. Preserve "
                    "the narrative core and every upstream block and beat field exactly; "
                    "correct only the invalid B2 visual assignments, scene prompts, "
                    "video_prompt values, reference/audio flags or descriptions. "
                    "Follow the supplied output schema.\n"
                    "Previous B2 response:\n"
                    f"{_payload(output.model_dump())}"
                )
                try:
                    retry_output = await call_b2(correction_suffix + retry_suffix)
                except ValidationError as retry_schema_error:
                    if on_rejected_b2_output is not None:
                        on_rejected_b2_output(
                            block.block_id,
                            rejection_attempt_offset + 2,
                            str(retry_schema_error),
                            schema_error_payload(retry_schema_error),
                        )
                    raise BPipelineValidationError(
                        f"B2 block {block.block_id} failed schema validation on retry: "
                        f"{retry_schema_error}"
                    ) from retry_schema_error
                try:
                    validate_visuals(retry_output, b2_input)
                except BPipelineValidationError as retry_error:
                    if on_rejected_b2_output is not None:
                        on_rejected_b2_output(
                            block.block_id, rejection_attempt_offset + 2,
                            str(retry_error), retry_output.model_dump()
                        )
                    raise BPipelineValidationError(
                        f"B2 block {block.block_id} failed validation after one retry: "
                        f"{retry_error}"
                    ) from retry_error
                output = retry_output
        if on_output is not None:
            on_output("B2", block.block_id, output.model_dump())
        return output

    retry_ids = set(retry_b2_block_ids or ())
    if retry_ids:
        if previous_b2 is None or len(previous_b2) != len(materialized):
            raise ValueError("B2 block retries require a complete validated B2 checkpoint")
        previous_by_id = {item.blocks[0].block_id: item for item in previous_b2}
        expected_ids = set(range(1, len(materialized) + 1))
        if set(previous_by_id) != expected_ids or not retry_ids <= expected_ids:
            raise BPipelineValidationError("B2 retry checkpoint block IDs are invalid")
    else:
        previous_by_id = {}

    b2_started = perf_counter()
    try:
        generated_b2 = await asyncio.gather(
            *(
                b2_one(meta, block, b12)
                for meta, block, b12 in zip(b11.blocks, materialized, b12_results, strict=True)
                if not retry_ids or meta.block_id in retry_ids
            )
        )
        if retry_ids:
            generated_by_id = {item.blocks[0].block_id: item for item in generated_b2}
            b2_results = [
                generated_by_id.get(block_id, previous_by_id[block_id])
                for block_id in range(1, len(materialized) + 1)
            ]
        else:
            b2_results = list(generated_b2)

        # Parallel block calls cannot see presenter runs crossing block boundaries.
        # Validate the ordered join and repair only the relevant block if needed.
        global_corrections: dict[int, int] = {}
        while (overflow := _overlong_avatar_run(b2_results)) is not None:
            run, total = overflow
            offset = 0
            candidates: list[tuple[int, int, int, str]] = []
            for beat in run:
                left = offset
                right = total - left - beat.characters
                offset += beat.characters
                if global_corrections.get(beat.block_id, 0) < 2:
                    candidates.append(
                        (
                            max(left, right),
                            global_corrections.get(beat.block_id, 0),
                            beat.block_id,
                            beat.beat_id,
                        )
                    )
            if not candidates:
                raise BPipelineValidationError(
                    _avatar_run_error(run, total)
                    + "; no correctable discretionary B2 beat remains"
                )
            _, _, block_id, target_beat_id = min(candidates)
            previous = b2_results[block_id - 1]
            correction_number = global_corrections.get(block_id, 0) + 1
            # Reserve attempts 1/2 for local validation and 3 slots per
            # whole-video correction so the runner retains all rejected JSON.
            global_attempt = 3 + (correction_number - 1) * 3
            error = _avatar_run_error(run, total)
            if on_rejected_b2_output is not None:
                on_rejected_b2_output(
                    block_id, global_attempt, error, previous.model_dump()
                )
            global_corrections[block_id] = correction_number
            suffix = (
                "\n\nB2 whole-video avatar-run correction. The other blocks have "
                "already been assigned. The joined result violates a hard limit: "
                f"{error}. Change the visual assignment of beat {target_beat_id} "
                "in YOUR block to a faithful media_image or media_video visual "
                "(not avatar_media), or break this run with another discretionary "
                "beat in your block. Keep every frozen beat text unchanged. "
                "Preserve every other valid visual "
                "decision where possible. Return only the corrected complete "
                "B2 block. Previous response for this block:\n"
                f"{_payload(previous.model_dump())}"
            )
            index = block_id - 1
            b2_results[index] = await b2_one(
                b11.blocks[index], materialized[index], b12_results[index],
                suffix, global_attempt,
            )
    finally:
        if on_stage_duration is not None:
            on_stage_duration("B2", perf_counter() - b2_started)
    result = BPipelineResult(
        b11=b11,
        b12=tuple(b12_results),
        b2=tuple(b2_results),
        visual_style=visual_style,
    )
    if on_stage_complete is not None:
        on_stage_complete("B2", len(b2_results), result.merged_b2_output())
    return result
