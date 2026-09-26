"""B1.1 → independent B1.2/B2 per-block workers, with exact source integrity.

This module deliberately does not convert the new string beat IDs or four visual
formats to the legacy integer-shot production pipeline. That needs a separate,
explicitly versioned downstream migration before any production cutover.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files

from ai_video_factory.providers.base import StructuredTextProvider

from .contracts import (
    B11Output,
    B12Input,
    B12Output,
    B2Input,
    B2InputBlock,
    B2Output,
    Beat,
    BlockMeta,
    MaterializedBlock,
)

PROMPT_CHECKSUMS = {
    "b1_1.md": "53a8539167a67c0282242e23ccf4984f0a188c38982d713d701c2b1d4baa8eb1",
    "b1_2.md": "02e58881e4654badb2299cb51f48dbea744364189b332a5942ce0c360b45c634",
    "b2.md": "1029a042a4442506b5c038cb061184b40dcf3e1fedd5abc0dfe588ff372c1903",
}
# Transport-only adaptation to Responses API Structured Outputs; all audited
# semantic and schema requirements in the original prompts remain unchanged.
_STRUCTURED_TRANSPORT = (
    "\n\nAPI transport: this call uses Responses API Structured Outputs. Return the "
    "specified JSON object directly via the supplied output schema, without an "
    "outer Markdown code fence. All schema, source-integrity and error rules apply."
)


class BPipelineValidationError(ValueError):
    """A model response violated its source-preserving stage contract."""


def _instructions(name: str) -> str:
    data = files("ai_video_factory.bots.prompts").joinpath(name).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != PROMPT_CHECKSUMS[name]:
        raise BPipelineValidationError(f"Audited prompt checksum mismatch: {name}")
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


def materialize_blocks(script: str, blocks: list[BlockMeta]) -> list[MaterializedBlock]:
    """Resolve globally unique verbatim anchors and tile the raw script exactly."""
    if not isinstance(script, str) or not script.strip():
        raise BPipelineValidationError("A nonempty authoritative raw script is required")
    if not blocks or [block.block_id for block in blocks] != list(range(1, len(blocks) + 1)):
        raise BPipelineValidationError("B1.1 block IDs must be consecutive from 1")

    starts: list[int] = []
    for block in blocks:
        first, last = block.span.first_words, block.span.last_words
        if first[0].isspace() or last[-1].isspace():
            raise BPipelineValidationError("B1.1 anchors must begin/end at substantive text")
        if script.count(first) != 1 or script.count(last) != 1:
            raise BPipelineValidationError(
                f"B1.1 block {block.block_id} anchor is not globally unique in raw script"
            )
        starts.append(script.index(first))
    if starts != sorted(set(starts)) or script[: starts[0]].strip():
        raise BPipelineValidationError("B1.1 anchors must start at first substantive text in order")

    result: list[MaterializedBlock] = []
    for index, block in enumerate(blocks):
        start = 0 if index == 0 else starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(script)
        first_position = starts[index]
        last = block.span.last_words
        last_position = script.index(last)
        if (
            not start <= first_position <= last_position < end
            or script[start:first_position].strip()
            or script[last_position + len(last) : end].strip()
        ):
            raise BPipelineValidationError(
                f"B1.1 block {block.block_id} span does not cover its exact substantive range"
            )
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
        if number > 1 and beat.text[0].isspace():
            raise BPipelineValidationError("B1.2 separator whitespace belongs to preceding beat")
        if not beat.text.strip() or len(beat.text.split()) > 40:
            raise BPipelineValidationError(f"Invalid B1.2 beat text or length: {beat.beat_id}")
    if "".join(beat.text for beat in result.beats) != block.text:
        raise BPipelineValidationError("B1.2 beats do not reconstruct the materialized block")
    return result.beats


def validate_visuals(result: B2Output, supplied: B2Input) -> None:
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
        restored = visual.model_dump(exclude={"visual_type", "description"})
        if restored != upstream.model_dump():
            raise BPipelineValidationError("B2 changed a frozen B1.2 beat")
    if expected.type == "intro" and actual.beats[0].visual_type != "avatar":
        raise BPipelineValidationError("B2 must anchor intro opening with avatar")
    if expected.type == "close" and actual.beats[-1].visual_type != "avatar":
        raise BPipelineValidationError("B2 must anchor close ending with avatar")


@dataclass(frozen=True)
class BPipelineResult:
    b11: B11Output
    b12: tuple[B12Output, ...]
    b2: tuple[B2Output, ...]

    def visual_plan(self) -> dict[str, object]:
        """Application-owned ordered join, not a bot-owned B2 output object."""
        assert self.b11.narrative_core is not None
        return {
            "schema_version": "b-pipeline-v1",
            "narrative_core": self.b11.narrative_core.model_dump(),
            "blocks": [result.blocks[0].model_dump() for result in self.b2 if result.blocks],
        }


async def run_b_pipeline(
    script: str,
    *,
    provider: StructuredTextProvider,
    model: str = "gpt-6-luna",
    max_parallel_calls: int = 8,
) -> BPipelineResult:
    """Run each new bot with the exact audited prompts and deterministic join order."""
    if not script or not script.strip():
        raise BPipelineValidationError("Authoritative script must not be empty")
    if max_parallel_calls < 1:
        raise ValueError("max_parallel_calls must be at least 1")
    b11 = await provider.generate_structured(
        model=model,
        instructions=_instructions("b1_1.md"),
        input_text=_payload({"plain_script_for_recording": script}),
        output_type=B11Output,
    )
    _require_success("B1.1", b11.error)
    if b11.narrative_core is None or b11.blocks is None:
        raise BPipelineValidationError("B1.1 did not provide required successful fields")
    materialized = materialize_blocks(script, b11.blocks)
    semaphore = asyncio.Semaphore(max_parallel_calls)

    # Two independent parallel waves: no B2 starts until every B1.2 succeeds.
    async def b12_one(block: MaterializedBlock) -> B12Output:
        b12_input = B12Input(
            narrative_core=b11.narrative_core,
            current_block_id=block.block_id,
            blocks=(block,),
        )
        async with semaphore:
            output = await provider.generate_structured(
                model=model,
                instructions=_instructions("b1_2.md"),
                input_text=_payload(b12_input.model_dump()),
                output_type=B12Output,
            )
        validate_beats(output, block)
        return output

    b12_results = await asyncio.gather(*(b12_one(block) for block in materialized))

    async def b2_one(
        meta: BlockMeta, block: MaterializedBlock, b12: B12Output
    ) -> B2Output:
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
        async with semaphore:
            output = await provider.generate_structured(
                model=model,
                instructions=_instructions("b2.md"),
                input_text=_payload(b2_input.model_dump()),
                output_type=B2Output,
            )
        validate_visuals(output, b2_input)
        return output

    b2_results = await asyncio.gather(
        *(
            b2_one(meta, block, b12)
            for meta, block, b12 in zip(b11.blocks, materialized, b12_results, strict=True)
        )
    )
    return BPipelineResult(
        b11=b11,
        b12=tuple(b12_results),
        b2=tuple(b2_results),
    )
