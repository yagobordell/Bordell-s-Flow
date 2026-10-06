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
from dataclasses import dataclass, field
from importlib.resources import files
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ValidationError

from ai_video_factory.config import settings
from ai_video_factory.providers.base import StructuredTextProvider
from ai_video_factory.providers.image_grids import (
    BLACK_PANEL_SOURCE,
    GridPlanError,
    plan_image_jobs,
)
from ai_video_factory.providers.openai_batch_images import described_beats

from .contracts import (
    B2Input,
    B2InputBlock,
    B2Output,
    B3Output,
    B11Output,
    B12Input,
    B12Output,
    Beat,
    BlockMeta,
    MaterializedBlock,
    ProfessionalB2Output,
)
from .cta import CTAPlacement, validate_placement
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


@dataclass(frozen=True)
class _B3GridLock:
    grid_id: str
    source: str
    role: Literal["speaker_start", "support"]


def _professional_grid_locks(
    outputs: list[ProfessionalB2Output] | tuple[ProfessionalB2Output, ...],
) -> dict[tuple[int, str], _B3GridLock]:
    """Freeze the exact B2 grid assignment before B3 can plan camera takes."""
    plan = {
        "visual_style": "profesional",
        "blocks": [item.blocks[0].model_dump() for item in outputs if item.blocks],
    }
    jobs = plan_image_jobs(plan, described_beats(plan))
    beats = {
        (block["block_id"], beat["beat_id"]): beat
        for block in plan["blocks"] for beat in block["beats"]
    }
    locks: dict[tuple[int, str], _B3GridLock] = {}
    for job in jobs:
        if job.get("grid") is None:
            continue
        grid_id = f"{job['block_id']}:{job['beat_id']}"
        for panel in job["panels"]:
            if panel.get("source") == BLACK_PANEL_SOURCE:
                continue
            key = panel["block_id"], panel["beat_id"]
            beat = beats[key]
            role: Literal["speaker_start", "support"] = (
                "support"
                if beat["visual_type"] == "avatar_media"
                else "speaker_start"
            )
            locks[key] = _B3GridLock(
                grid_id=grid_id,
                source=str(panel.get("source") or "filler"),
                role=role,
            )
    return locks


def _professional_audio_groups(
    outputs: list[ProfessionalB2Output] | tuple[ProfessionalB2Output, ...],
) -> list[list[tuple[int, dict[str, object]]]]:
    """Return maximal whole-video runs of consecutive audio=true beats."""
    groups: list[list[tuple[int, dict[str, object]]]] = []
    current: list[tuple[int, dict[str, object]]] = []
    for output in outputs:
        if not output.blocks:
            raise BPipelineValidationError("Professional B2 output is missing its block")
        block = output.blocks[0]
        for beat in block.beats:
            if beat.audio:
                current.append((block.block_id, beat.model_dump()))
            elif current:
                groups.append(current)
                current = []
    if current:
        groups.append(current)
    return groups


def validate_b3(
    result: B3Output,
    *,
    group_id: int,
    source: list[tuple[int, dict[str, object]]],
    grid_locks: dict[tuple[int, str], _B3GridLock],
) -> None:
    """B3 may choose cuts only; B2 narration and fixed grids remain authoritative."""
    _require_success("B3", result.error)
    if result.group_id != group_id or result.beats is None:
        raise BPipelineValidationError("B3 returned the wrong spoken group")
    expected_ids = [str(beat["beat_id"]) for _block_id, beat in source]
    if [beat.beat_id for beat in result.beats] != expected_ids:
        raise BPipelineValidationError("B3 changed spoken beat IDs or order")
    if result.beats[0].continuous:
        raise BPipelineValidationError("The first B3 beat must start a take")
    for index, ((block_id, original), decision) in enumerate(
        zip(source, result.beats, strict=True)
    ):
        if original.get("audio") is not True:
            raise BPipelineValidationError("B3 received a beat without audio=true")
        lock = grid_locks.get((block_id, decision.beat_id))
        if lock is not None and lock.role == "speaker_start":
            if decision.continuous or decision.shot_prompt is not None:
                raise BPipelineValidationError(
                    f"B3 grid speaker beat {decision.beat_id} must start from its fixed panel"
                )
        elif not decision.continuous and index > 0:
            if not isinstance(decision.shot_prompt, str) or not decision.shot_prompt.strip():
                raise BPipelineValidationError(
                    f"B3 new take {decision.beat_id} requires a continuity shot_prompt"
                )
        if (
            isinstance(decision.shot_prompt, str)
            and re.match(r"^\s*[1-9]\.\s+", decision.shot_prompt)
        ):
            raise BPipelineValidationError(
                f"B3 shot_prompt {decision.beat_id} must not create grid numbering"
            )


@dataclass(frozen=True)
class BPipelineResult:
    b11: B11Output
    b12: tuple[B12Output, ...]
    b2: tuple[B2Output | ProfessionalB2Output, ...]
    b3: tuple[B3Output, ...] = ()
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
            "blocks": [item.blocks[0].model_dump() for item in self.b2 if item.blocks],
        }

    def merged_b3_output(self) -> dict[str, object]:
        """Application-owned join of whole-video spoken-run B3 decisions."""
        return {
            "schema_version": "b-pipeline-merged-v1",
            "pipeline_stage": "B3",
            "groups": [item.model_dump() for item in self.b3],
        }

    def visual_plan(self) -> dict[str, object]:
        """Application-owned ordered B2 plan enriched with B3 camera-take decisions."""
        if self.b11.narrative_core is None:
            raise BPipelineValidationError("B1.1 narrative core is missing")
        blocks = [result.blocks[0].model_dump() for result in self.b2 if result.blocks]
        if self.b3:
            if self.visual_style != "profesional":
                raise BPipelineValidationError("B3 is valid only for professional visuals")
            professional = tuple(
                item for item in self.b2 if isinstance(item, ProfessionalB2Output)
            )
            if len(professional) != len(self.b2):
                raise BPipelineValidationError("Professional B3 requires professional B2")
            locks = _professional_grid_locks(professional)
            groups = _professional_audio_groups(professional)
            if len(groups) != len(self.b3):
                raise BPipelineValidationError("B3 group count differs from B2 audio runs")
            annotations: dict[tuple[int, str], dict[str, object]] = {}
            for group_id, (source, output) in enumerate(
                zip(groups, self.b3, strict=True), start=1
            ):
                validate_b3(
                    output, group_id=group_id, source=source, grid_locks=locks
                )
                decisions = output.beats
                if decisions is None:
                    raise BPipelineValidationError("Validated B3 output has no beats")
                for index, ((block_id, _original), decision) in enumerate(
                    zip(source, decisions, strict=True)
                ):
                    previous_id = (
                        str(source[index - 1][1]["beat_id"]) if index > 0 else None
                    )
                    lock = locks.get((block_id, decision.beat_id))
                    annotations[(block_id, decision.beat_id)] = {
                        "b3_group_id": group_id,
                        "continuous": decision.continuous,
                        "b3_shot_prompt": decision.shot_prompt,
                        "b3_shot_video_prompt": decision.shot_video_prompt,
                        "b3_reference_beat_id": (
                            previous_id if decision.shot_prompt is not None else None
                        ),
                        **(
                            {
                                "b3_grid_id": lock.grid_id,
                                "b3_grid_source": lock.source,
                                "b3_grid_role": lock.role,
                            }
                            if lock is not None else {}
                        ),
                    }
            for block in blocks:
                for beat in block["beats"]:
                    beat.update(annotations.get((block["block_id"], beat["beat_id"]), {}))
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


async def run_b_pipeline(
    script: str,
    *,
    provider: StructuredTextProvider,
    model: str | None = None,
    max_parallel_calls: int = 8,
    visual_style: Literal["casero", "profesional"] = "casero",
    enable_casero_cta: bool = False,
    avatar_image: bytes | None = None,
    start_from: Literal["B1.1", "B1.2", "B2"] = "B1.1",
    stop_after: Literal["B1.1", "B1.2", "B2", "B3"] = "B2",
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
    on_rejected_b3_output: (
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
    if stop_after in {"B2", "B3"}:
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
    if stop_after not in {"B1.1", "B1.2", "B2", "B3"}:
        raise ValueError(f"Unknown stop stage: {stop_after}")
    if stop_after == "B3" and visual_style != "profesional":
        raise ValueError("B3 is available only for professional visual style")
    stage_order = ("B1.1", "B1.2", "B2", "B3")
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
        # Two independent parallel waves: no B2 starts until every B1.2 succeeds.
        async def b12_one(block: MaterializedBlock) -> B12Output:
            if previous_partial_b12 and block.block_id in previous_partial_b12:
                saved = previous_partial_b12[block.block_id]
                validate_beats(saved, block)
                if on_output is not None:
                    on_output("B1.2", block.block_id, saved.model_dump())
                return saved
            b12_input = B12Input(
                narrative_core=b11.narrative_core,
                current_block_id=block.block_id,
                blocks=(block,),
            )
            last_response_id: str | None = None

            def remember_response(response_id: str) -> None:
                nonlocal last_response_id
                last_response_id = response_id

            async def call_b12(
                feedback: str = "", previous_response_id: str | None = None,
            ) -> B12Output:
                async with semaphore:
                    return await _call_stage(
                        "B1.2",
                        block.block_id,
                        b12_input.model_dump(),
                        provider=provider,
                        model=model,
                        prompt="b1_2.md",
                        instructions_suffix="\n\n" + feedback if feedback else "",
                        correction_feedback=feedback or None,
                        previous_response_id=previous_response_id,
                        on_response_id=remember_response,
                        output_type=B12Output,
                        on_input=on_input,
                        on_api_response=on_api_response,
                        on_call_duration=on_call_duration,
                    )

            feedback = ""
            previous_response_id: str | None = None
            for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
                candidate: B12Output | None = None
                try:
                    candidate = await call_b12(feedback, previous_response_id)
                    # A declared invalid upstream input is not correctable by B1.2.
                    if candidate.error is not None:
                        _require_success("B1.2", candidate.error)
                    validate_beats(candidate, block)
                    if attempt > 1 and "".join(
                        beat.text for beat in candidate.beats or []
                    ) != block.text:
                        raise BPipelineValidationError(
                            "B1.2 corrected beats do not reconstruct the original block exactly"
                        )
                    output = candidate
                    break
                except (ValidationError, BPipelineValidationError, RuntimeError) as error:
                    if isinstance(error, RuntimeError) and not is_schema_failure(error):
                        raise
                    if candidate is not None and candidate.error is not None:
                        # Re-run the upstream stage instead of modifying frozen input.
                        raise
                    rejected = candidate.model_dump() if candidate is not None else {
                        "unparsed_response": True, "validation_error": str(error),
                    }
                    if on_rejected_b12_output is not None:
                        on_rejected_b12_output(
                            block.block_id, attempt, str(error), rejected,
                        )
                    if attempt > MAX_VALIDATION_RETRIES:
                        raise BPipelineValidationError(
                            f"B1.2 block {block.block_id} failed validation after "
                            f"{MAX_VALIDATION_RETRIES} retries: {error}"
                        ) from error
                    previous_response_id = (
                        last_response_id
                        if getattr(provider, "supports_response_continuation", False)
                        else None
                    )
                    feedback = validation_feedback(
                        "B1.2", error, block_id=block.block_id,
                        previous=candidate.model_dump() if candidate else None,
                        continued=previous_response_id is not None,
                        instructions=(
                            "B1.2 automatic beat-length correction when lengths are invalid. "
                            "Previous invalid B1.2 response: correct this block only. "
                            "Keep every source word in the same order without insertion, "
                            "deletion or replacement. Return consecutive beat IDs. "
                            "Use at most 40 words and 100 characters per beat, at least "
                            "13 characters except for a single unavoidable short block. "
                            "Split long beats, merge short beats with a neighbor and "
                            "adjust boundaries if necessary. The concatenation of ALL "
                            "returned beat texts MUST equal the frozen input block "
                            "character for character, including punctuation and spaces."
                        ),
                    )
            if on_output is not None:
                on_output("B1.2", block.block_id, output.model_dump())
            return output

        b12_started = perf_counter()
        try:
            b12_outcomes = await asyncio.gather(
                *(b12_one(block) for block in materialized),
                return_exceptions=True,
            )
            b12_failures = [
                item for item in b12_outcomes if isinstance(item, BaseException)
            ]
            if b12_failures:
                # Successful block outputs are already persisted for --from B1.2.
                raise b12_failures[0]
            b12_results = [item for item in b12_outcomes if isinstance(item, B12Output)]
        finally:
            if on_stage_duration is not None:
                on_stage_duration("B1.2", perf_counter() - b12_started)
        if on_stage_complete is not None:
            partial = BPipelineResult(b11=b11, b12=tuple(b12_results), b2=())
            on_stage_complete("B1.2", len(b12_results), partial.merged_b12_output())
    if stop_after == "B1.2":
        return BPipelineResult(b11=b11, b12=tuple(b12_results), b2=())

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
                        "For professional style, audio=true is reserved for the "
                        "narrator while preserving the reference narration setup and "
                        "the narrator's physical position within that setup. A generated "
                        "full-screen narrator who stays in that setup must use "
                        "media_video with reference=true and audio=true; if the narrator "
                        "leaves that setup or changes position substantially, use "
                        "audio=false voice-over coverage. media_image must have "
                        "audio=false. Do not change any valid visual decision unnecessarily. "
                        + (
                            "For professional B2, only a reused avatar may have a null "
                            "prompt; all other visuals require a nonempty prompt. "
                            "media_image ALWAYS requires audio=false; media_video requires "
                            "a nonempty video_prompt for synchronized speech. "
                            "The FIRST beat of an intro and LAST beat of a close must "
                            "reuse the original avatar without regeneration."
                            if visual_style == "profesional" else ""
                        )
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

    b2_started = perf_counter()
    try:
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
        while (overflow := _overlong_avatar_run(b2_results)) is not None:
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
                mandatory = visual_style == "profesional" and (
                    (current_block.type == "intro" and index == 0)
                    or (current_block.type == "close"
                        and index == len(current_block.beats) - 1)
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
                    block_id, b2_call_numbers[block_id], str(error), previous.model_dump()
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
                        "within that setup. Never change mandatory professional "
                        "intro/close anchors. Keep all frozen narration and upstream "
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
        if visual_style == "profesional" and stop_after == "B3":
            # B3 must see the exact immutable grid assignment, including fillers.
            # Repair invalid B2 numbering now, before any B3 model call or paid image work.
            while True:
                professional_results = [
                    item for item in b2_results
                    if isinstance(item, ProfessionalB2Output)
                ]
                if len(professional_results) != len(b2_results):
                    raise BPipelineValidationError(
                        "Professional B3 requires professional B2 outputs"
                    )
                try:
                    _professional_grid_locks(professional_results)
                    break
                except GridPlanError as error:
                    match = re.search(r"(?:at |description at )(\d+)[A-Z]+", str(error))
                    if match is None:
                        raise BPipelineValidationError(
                            f"B2 grid preflight failed before B3: {error}"
                        ) from error
                    block_id = int(match.group(1))
                    if b2_correction_counts.get(block_id, 0) >= MAX_VALIDATION_RETRIES:
                        raise BPipelineValidationError(
                            f"B2 block {block_id} exhausted retries before B3 grid preflight"
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
                            "B3 has not run yet and must receive the exact final grid plan."
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

    result = BPipelineResult(
        b11=b11,
        b12=tuple(b12_results),
        b2=tuple(b2_results),
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
        raise BPipelineValidationError("B3 requires professional B2 outputs")
    grid_locks = _professional_grid_locks(professional_results)
    audio_groups = _professional_audio_groups(professional_results)

    async def b3_one(
        group_id: int,
        source: list[tuple[int, dict[str, object]]],
    ) -> B3Output:
        payload_beats: list[dict[str, object]] = []
        for block_id, beat in source:
            lock = grid_locks.get((block_id, str(beat["beat_id"])))
            payload_beats.append({
                "block_id": block_id,
                **beat,
                "grid_id": lock.grid_id if lock is not None else None,
                "grid_source": lock.source if lock is not None else None,
                "grid_role": lock.role if lock is not None else None,
            })
        payload: dict[str, object] = {
            "narrative_core": b11.narrative_core.model_dump(),
            "group_id": group_id,
            "beats": payload_beats,
        }
        feedback = ""
        for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
            candidate: B3Output | None = None
            try:
                async with semaphore:
                    candidate = await _call_stage(
                        "B3",
                        group_id,
                        payload,
                        provider=provider,
                        model=model,
                        prompt="b3_professional.md",
                        instructions_suffix="\n\n" + feedback if feedback else "",
                        output_type=B3Output,
                        input_image_url=image_url,
                        on_input=on_input,
                        on_api_response=on_api_response,
                        on_call_duration=on_call_duration,
                    )
                validate_b3(
                    candidate,
                    group_id=group_id,
                    source=source,
                    grid_locks=grid_locks,
                )
                if on_output is not None:
                    on_output("B3", group_id, candidate.model_dump())
                return candidate
            except (ValidationError, BPipelineValidationError, RuntimeError) as error:
                if isinstance(error, RuntimeError) and not is_schema_failure(error):
                    raise
                rejected = candidate.model_dump() if candidate is not None else {
                    "unparsed_response": True,
                    "validation_error": str(error),
                }
                if on_rejected_b3_output is not None:
                    on_rejected_b3_output(group_id, attempt, str(error), rejected)
                if attempt > MAX_VALIDATION_RETRIES:
                    raise BPipelineValidationError(
                        f"B3 group {group_id} failed validation after "
                        f"{MAX_VALIDATION_RETRIES} retries: {error}"
                    ) from error
                feedback = validation_feedback(
                    "B3 profesional",
                    error,
                    block_id=group_id,
                    previous=candidate.model_dump() if candidate else None,
                    instructions=(
                        "Repair only the B3 camera-continuity decisions. "
                        "Do not change beat IDs or B2 decisions. The first beat starts "
                        "a take; fixed grid speaker-start beats also start takes and "
                        "must keep shot_prompt=null. Every other non-first new take "
                        "requires a continuity shot_prompt using the preceding effective "
                        "shot as reference. continuous=true has no shot prompts."
                    ),
                )
        raise AssertionError("Unreachable B3 retry state")

    b3_started = perf_counter()
    try:
        outcomes = await asyncio.gather(
            *(b3_one(index, group) for index, group in enumerate(audio_groups, start=1)),
            return_exceptions=True,
        )
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        if failures:
            raise failures[0]
        b3_results = tuple(item for item in outcomes if isinstance(item, B3Output))
    finally:
        if on_stage_duration is not None:
            on_stage_duration("B3", perf_counter() - b3_started)

    result = BPipelineResult(
        b11=b11,
        b12=tuple(b12_results),
        b2=tuple(b2_results),
        b3=b3_results,
        visual_style=visual_style,
        b2_correction_counts=b2_correction_counts,
        b2_response_ids=b2_last_response_ids,
    )
    if on_stage_complete is not None:
        on_stage_complete("B3", len(b3_results), result.merged_b3_output())
    return result
