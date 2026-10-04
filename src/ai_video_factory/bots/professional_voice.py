"""Professional post-B2 V1/V2/V3 direction and block-local Gemini 3.8 speech.

B2 is the source of visual context, never a source of extra spoken words.
All paid operations have durable state; an ambiguous Gemini POST is NEVER replayed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_video_factory.bots.audio_join import join_audio_blocks
from ai_video_factory.bots.fish_audio_workflow import _transcribe_block
from ai_video_factory.config import settings
from ai_video_factory.providers.gemini_tts import (
    DEFAULT_GEMINI_TTS_MODEL,
    GeminiTtsSpeechProvider,
)
from ai_video_factory.providers.openai_stt import OpenAISttClient


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Tempo = Literal["held", "measured", "driving", "relentless"]
Pressure = Literal["low", "moderate", "high", "extreme"]
Modulation = Literal["below", "at", "above"]
Tag = Literal["breath", "exhales", "sigh", "chuckle", "gasp", "short pause", "long pause"]
Ending = Literal["carry", "resolve", "land", "clipped", "trail_off", "hang"]


class BlockRhythm(Strict):
    block_id: int
    phase: str = Field(min_length=1)
    viewer_trajectory: str = Field(min_length=1)
    tempo: Tempo
    pressure: Pressure
    contrast: str = Field(min_length=1)


class Reconsideration(Strict):
    block_id: int
    axis: Literal["tempo", "pressure"]
    finding: str = Field(min_length=1)


class V1(Strict):
    pipeline_stage: Literal["V1"]
    curve: str = Field(min_length=1)
    block_rhythms: list[BlockRhythm]
    baseline_reconsiderations: list[Reconsideration]


class BeatIntent(Strict):
    beat_id: str
    function: str = Field(min_length=1)
    understanding_delta: str = Field(min_length=1)
    viewer_trajectory: str = Field(min_length=1)
    tempo_modulation: Modulation
    pressure_modulation: Modulation


class Misfit(Strict):
    axis: Literal["tempo", "pressure"]
    beat_ids: list[str] = Field(min_length=1)
    reading: str = Field(min_length=1)


class V2(Strict):
    pipeline_stage: Literal["V2"]
    block_id: int
    beat_intents: list[BeatIntent]
    landing_beat_id: str
    baseline_misfit: list[Misfit]


HumanBehavior = Literal["inhale", "exhale", "sigh", "chuckle", "gasp"]
PauseClass = Literal["breath", "beat", "hold"]


class VocalIntent(Strict):
    """Artistic humanization intent; the V4 compiler alone selects inline tags."""
    at: int = Field(
        ge=0, description="Semantic location within the verbatim spoken fragment; no inline tag.",
    )
    behavior: HumanBehavior


class Speak(Strict):
    speak: str = Field(min_length=1)
    delivery_quality: str = Field(
        min_length=1,
        description=(
            "Natural-language artistic delivery only. "
            "No technical tags, markup or Gemini commands."
        ),
    )
    ending_behavior: Ending
    stress: list[str]
    vocal_intents: list[VocalIntent]


class Pause(Strict):
    pause: PauseClass
    pause_function: str = Field(
        min_length=1,
        description=(
            "Artistic purpose of deliberate silence, in prose only. "
            "No tags or technical syntax."
        ),
    )


class BeatDelivery(Strict):
    beat_id: str
    delivery: list[Speak | Pause] = Field(min_length=1)


class BlockPerformance(Strict):
    register_: str = Field(
        alias="register", serialization_alias="register", min_length=1,
        description="Stable artistic vocal register; no technical inline codes or tags.",
    )
    pace_character: str = Field(
        min_length=1, description="Qualitative artistic pace, natural prose without inline tags.",
    )
    intensity_arc: str = Field(
        min_length=1, description="Artistic intensity movement, no technical markup.",
    )


class V3(Strict):
    pipeline_stage: Literal["V3"]
    block_id: int
    block_performance: BlockPerformance
    beat_deliveries: list[BeatDelivery]


class V4SpokenDirection(Strict):
    """A locatable clause in ONE group-wide style, not a Gemini turn."""
    beat_id: str
    source_item_index: int = Field(ge=0)
    locator: str = Field(min_length=1)
    occurrence: int = Field(default=1, ge=1)
    instruction: str = Field(min_length=1, max_length=260)
    ending_behavior: Ending
    stress: list[str]


class V4TagSnippet(Strict):
    """Only adjacent words and one tag, anchored to canonical B2 by offset."""
    at: int = Field(ge=0)
    tag: Tag
    text: str = Field(min_length=1)
    source_kind: Literal["vocal_intent", "pause"]
    source_item_index: int = Field(ge=0)
    source_at: int | None = Field(default=None, ge=0)
    pause_direction: str | None = Field(default=None, min_length=1, max_length=160)


class V4BeatTags(Strict):
    beat_id: str
    text: Literal[""] = ""
    inserts: list[V4TagSnippet]


class V4(Strict):
    pipeline_stage: Literal["V4"]
    block_id: int
    audio_profile: str = Field(min_length=1, max_length=200)
    directors_notes: list[str] = Field(min_length=1)
    spoken_directions: list[V4SpokenDirection]
    beat_tags: list[V4BeatTags]


class VoicePlanError(ValueError):
    """A V stage returned semantically invalid or unsafe direction."""


_TEMPO = ("held", "measured", "driving", "relentless")
_PRESSURE = ("low", "moderate", "high", "extreme")


def _json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise VoicePlanError(f"Unsafe voice state path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.is_symlink():
        raise VoicePlanError(f"Unsafe temporary voice state: {tmp}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VoicePlanError(f"Missing or linked professional voice state: {path}")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise VoicePlanError(f"Malformed voice state: {path}")
    return result


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()


def _source_block_signature(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"block_id": block["block_id"],
         "beat_ids": [beat["beat_id"] for beat in block["beats"]]}
        for block in blocks
    ]


def _progressive_voice_groups(blocks: list[dict[str, Any]]) -> list[list[int]]:
    """Merge adjacent small B2 blocks from 1 through 4 beats, shortest neighbour first.

    Recompute sizes after every merge. For equal-sized neighbours choose the
    previous block, and process the earliest remaining candidate of each size.
    B2 itself is never changed.
    """
    if not blocks or len({block["block_id"] for block in blocks}) != len(blocks):
        raise VoicePlanError("Voice grouping requires distinct ordered B2 blocks")
    units: list[list[dict[str, Any]]] = [[block] for block in blocks]

    def count(unit: list[dict[str, Any]]) -> int:
        return sum(len(block["beats"]) for block in unit)

    for size in range(1, 5):
        while len(units) > 1:
            index = next((i for i, unit in enumerate(units) if count(unit) == size), None)
            if index is None:
                break
            if index == 0:
                target = 1
            elif index == len(units) - 1:
                target = index - 1
            else:
                target = (
                    index - 1 if count(units[index - 1]) <= count(units[index + 1])
                    else index + 1
                )
            left = min(index, target)
            units[left:left + 2] = [units[left] + units[left + 1]]
    return [[block["block_id"] for block in unit] for unit in units]


def _assemble_voice_blocks(
    blocks: list[dict[str, Any]], groups: list[list[int]],
) -> list[dict[str, Any]]:
    """Create voice-only containers, preserving every B2 beat and source boundary."""
    cursor = 0
    assembled: list[dict[str, Any]] = []
    for ids in groups:
        members = blocks[cursor:cursor + len(ids)]
        if not ids or [block["block_id"] for block in members] != ids:
            raise VoicePlanError("Voice groups must cover contiguous B2 blocks in order")
        cursor += len(ids)
        if len(members) == 1:
            assembled.append(members[0])
            continue
        first, last = members[0], members[-1]
        assembled.append({
            "block_id": first["block_id"],
            "type": "grouped",
            "emotional_entry": first["emotional_entry"],
            "emotional_exit": last["emotional_exit"],
            "span": {
                "first_words": first["span"]["first_words"],
                "last_words": last["span"]["last_words"],
            },
            "source_block_ids": ids,
            "source_blocks": [
                {
                    "block_id": block["block_id"], "type": block["type"],
                    "emotional_entry": block["emotional_entry"],
                    "emotional_exit": block["emotional_exit"], "span": block["span"],
                    "beat_ids": [beat["beat_id"] for beat in block["beats"]],
                }
                for block in members
            ],
            "beats": [beat for block in members for beat in block["beats"]],
        })
    if cursor != len(blocks):
        raise VoicePlanError("Voice groups omit one or more original B2 blocks")
    return assembled


def _voice_blocks_for_plan(
    blocks: list[dict[str, Any]], _output_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic groups without a separate grouping checkpoint."""
    return _assemble_voice_blocks(blocks, _progressive_voice_groups(blocks))


def _voice_blocks_for_audio(
    blocks: list[dict[str, Any]], output: Path,
) -> list[dict[str, Any]]:
    del output
    return _voice_blocks_for_plan(blocks)


def _check_v1(result: V1, blocks: list[dict[str, Any]], contested: list[dict[str, Any]]) -> None:
    ids = [block["block_id"] for block in blocks]
    if [entry.block_id for entry in result.block_rhythms] != ids:
        raise VoicePlanError(f"V1 must emit exactly these block IDs in order: {ids}")
    expected = {(entry["block_id"], entry["axis"]) for entry in contested}
    actual = {(entry.block_id, entry.axis) for entry in result.baseline_reconsiderations}
    if len(actual) != len(result.baseline_reconsiderations) or actual != expected:
        raise VoicePlanError(f"V1 baseline_reconsiderations keys must equal {sorted(expected)}")
    for block_id, axis in expected:
        history = [item["contested_value"] for item in contested
                   if item["block_id"] == block_id and item["axis"] == axis]
        actual_value = getattr(next(r for r in result.block_rhythms
                                    if r.block_id == block_id), axis)
        if actual_value in history[:-1]:
            raise VoicePlanError(f"V1 returned to retired {axis} baseline for block {block_id}")


def _check_v2(result: V2, block: dict[str, Any], rhythm: BlockRhythm) -> None:
    beat_ids = [beat["beat_id"] for beat in block["beats"]]
    if result.block_id != block["block_id"] or [x.beat_id for x in result.beat_intents] != beat_ids:
        raise VoicePlanError(
            f"V2 block {block['block_id']}: beat_ids must exactly match {beat_ids}"
        )
    if result.landing_beat_id not in beat_ids:
        raise VoicePlanError("V2 landing_beat_id is not a beat in this block")
    for axis, scale in (("tempo", _TEMPO), ("pressure", _PRESSURE)):
        base = scale.index(getattr(rhythm, axis))
        for beat in result.beat_intents:
            direction = getattr(beat, f"{axis}_modulation")
            step = {"below": -1, "at": 0, "above": 1}[direction]
            if len(beat_ids) == 1 and step:
                raise VoicePlanError(f"V2 one-beat block {block['block_id']} must be at/at")
            if not 0 <= base + step < len(scale):
                raise VoicePlanError(f"V2 {beat.beat_id} {axis} {direction} exceeds scale edge")
    axes = [item.axis for item in result.baseline_misfit]
    if len(set(axes)) != len(axes):
        raise VoicePlanError("V2 baseline_misfit repeats an axis")
    for item in result.baseline_misfit:
        if any(beat_id not in beat_ids for beat_id in item.beat_ids):
            raise VoicePlanError(f"V2 baseline_misfit {item.axis} contains unknown beat_ids")


def _check_v3(result: V3, block: dict[str, Any]) -> None:
    performance = result.block_performance
    if any(not text.strip() for text in (
        performance.register_, performance.pace_character, performance.intensity_arc,
    )):
        raise VoicePlanError("V3 block performance cannot contain empty prose")
    # V3 is the artistic director, never a Gemini token compiler.
    # Reject markup in ALL artistic prose; historically this was checked
    # only in the B2 text, so tags could leak through delivery/pause notes.
    for name, note in (
        ("register", performance.register_),
        ("pace_character", performance.pace_character),
        ("intensity_arc", performance.intensity_arc),
    ):
        if "<" in note or ">" in note:
            raise VoicePlanError(
                f"V3 inline-tag leakage in block_performance.{name}: "
                "describe the performance naturally; V4 alone emits angle-bracket tags"
            )
    beats = block["beats"]
    if result.block_id != block["block_id"] or [
        beat.beat_id for beat in result.beat_deliveries
    ] != [beat["beat_id"] for beat in beats]:
        raise VoicePlanError("V3 changed original beat identity or order")
    previous_outgoing_pause = False
    for source, delivery in zip(beats, result.beat_deliveries, strict=True):
        if (previous_outgoing_pause and delivery.delivery
            and isinstance(delivery.delivery[0], Pause)):
            raise VoicePlanError("V3 adjacent pauses across original beat boundary")
        previous_outgoing_pause = isinstance(delivery.delivery[-1], Pause)
        spoken = [item for item in delivery.delivery if isinstance(item, Speak)]
        if re.search(r"<[^>]+>", source["text"]):
            raise VoicePlanError("Ambiguous literal Gemini tags in source narration")
        rebuilt = "".join(item.speak for item in spoken)
        if not spoken or rebuilt != source["text"]:
            if re.search(r"<[^<>]*>", rebuilt):
                raise VoicePlanError(
                    f"V3 inline-tag leakage in beat {source['beat_id']} narration: "
                    "copy B2 text verbatim; only V4 may insert angle-bracket tags"
                )
            raise VoicePlanError(f"V3 altered verbatim narration in beat {source['beat_id']}")
        for index, item in enumerate(delivery.delivery):
            if isinstance(item, Pause):
                if not item.pause_function.strip():
                    raise VoicePlanError("V3 explicit pause needs an audible purpose")
                if "<" in item.pause_function or ">" in item.pause_function:
                    raise VoicePlanError(
                        f"V3 inline-tag leakage in beat {source['beat_id']} "
                        "pause_function: describe silence artistically; V4 owns tags"
                    )
                if (index and isinstance(delivery.delivery[index - 1], Pause)
                    or index == len(delivery.delivery) - 1
                    and item.pause == "breath"):
                    raise VoicePlanError("V3 adjacent pauses or trailing breath pause")
                continue
            if not item.delivery_quality.strip():
                raise VoicePlanError("V3 spoken delivery needs an audible direction")
            if "<" in item.delivery_quality or ">" in item.delivery_quality:
                raise VoicePlanError(
                    f"V3 inline-tag leakage in beat {source['beat_id']} "
                    "delivery_quality: describe human behavior without technical "
                    "tokens; V4 alone inserts tags"
                )
            if len(item.vocal_intents) > 2:
                raise VoicePlanError("V3 excessive vocal intents in spoken unit")
            if len(set(item.stress)) != len(item.stress) or any(
                not target or target not in item.speak for target in item.stress
            ):
                raise VoicePlanError("V3 stress must be exact unique source substrings")
            positions = [event.at for event in item.vocal_intents]
            if len(set(positions)) != len(positions):
                raise VoicePlanError("V3 duplicate vocal intent positions")
            for intent in item.vocal_intents:
                if intent.at > len(item.speak) or (
                    0 < intent.at < len(item.speak)
                    and item.speak[intent.at - 1].isalnum()
                    and item.speak[intent.at].isalnum()
                ):
                    raise VoicePlanError("V3 vocal intent splits a word or exceeds text")


def _minimal_tag_excerpt(text: str, at: int, tag: str) -> str:
    """Preserve one adjacent original word either side, including punctuation."""
    left, right = text[:at], text[at:]
    before = re.search(r"\w+[^\w]*$", left)
    after = re.match(r"[^\w]*\w+", right)
    return (
        (before.group() if before else left if not left.strip() else "")
        + f"<{tag}>"
        + (after.group() if after else right if not right.strip() else "")
    )


def _v4_style(direction: V4, source: V3) -> str:
    """Compile ALL directions for a group into exactly one Gemini style."""
    lines = [direction.audio_profile, *direction.directors_notes]
    all_speaks = [
        item.speak for beat in source.beat_deliveries
        for item in beat.delivery if isinstance(item, Speak)
    ]
    for item in direction.spoken_directions:
        matches = sum(speak.count(item.locator) for speak in all_speaks)
        label = (
            f"occurrence {item.occurrence} of '{item.locator}'"
            if matches > 1 else f"'{item.locator}'"
        )
        lines.append(f"On {label}: {item.instruction}")
    for v3_beat, tagged_beat in zip(
        source.beat_deliveries, direction.beat_tags, strict=True
    ):
        original = "".join(
            item.speak for item in v3_beat.delivery if isinstance(item, Speak)
        )
        cursor = 0
        for index, item in enumerate(v3_beat.delivery):
            if isinstance(item, Speak):
                cursor += len(item.speak)
                continue
            event = next(
                (e for e in tagged_beat.inserts
                 if e.source_kind == "pause" and e.source_item_index == index),
                None,
            )
            # An omitted technical tag must not silently discard designed
            # silence: default to V3's authored natural-language purpose.
            purpose = event.pause_direction if event else item.pause_function
            anchor = _minimal_tag_excerpt(original, cursor, "seam").replace(
                "<seam>", "|"
            )
            lines.append(
                f"At '{anchor}', pause class: {item.pause}; {purpose}"
            )
    return chr(10).join(lines)


def _check_v4(result: V4, block: dict[str, Any], direction: V3) -> None:
    """Verify every V3 decision and sparse tag against immutable original B2."""
    # A V4 compiler may not accept hidden technical markup from V3 prose.
    _check_v3(direction, block)
    ids = [beat["beat_id"] for beat in block["beats"]]
    if (result.block_id != direction.block_id
        or [entry.beat_id for entry in result.beat_tags] != ids
        or [entry.beat_id for entry in direction.beat_deliveries] != ids):
        raise VoicePlanError("V4 changed original beat identity or order")
    prose = [result.audio_profile, *result.directors_notes]
    if any(not note.strip() or re.search(r"<[^>]+>", note) for note in prose):
        raise VoicePlanError("V4 requires substantive tag-free group directions")
    if any(len(note) > 350 for note in result.directors_notes):
        raise VoicePlanError("V4 directors_notes must be concise")
    expected_spoken = [
        (beat.beat_id, index, item)
        for beat in direction.beat_deliveries
        for index, item in enumerate(beat.delivery)
        if isinstance(item, Speak)
    ]
    if [(entry.beat_id, entry.source_item_index) for entry in result.spoken_directions] != [
        (bid, index) for bid, index, _ in expected_spoken
    ]:
        raise VoicePlanError("V4 omitted, duplicated or reordered V3 spoken occurrences")
    for position, (entry, (_, _, original)) in enumerate(zip(
        result.spoken_directions, expected_spoken, strict=True
    )):
        if (not entry.instruction.strip() or entry.locator not in original.speak
            or original.speak.count(entry.locator) != 1
            or entry.ending_behavior != original.ending_behavior
            or entry.stress != original.stress
            or any(word not in entry.instruction for word in original.stress)
            or re.search(r"<[^>]+>", entry.instruction)):
            raise VoicePlanError("V4 lost exact spoken locator, stress or ending")
        occurrence = 1 + sum(
            earlier.speak.count(entry.locator)
            for _, _, earlier in expected_spoken[:position]
        )
        if entry.occurrence != occurrence:
            raise VoicePlanError("V4 locator lacks exact group-wide occurrence ordinal")
    voice_tags = {
        "inhale": "breath", "exhale": "exhales", "sigh": "sigh",
        "chuckle": "chuckle", "gasp": "gasp",
    }
    pause_tags = {
        "breath": {"short pause"}, "beat": {"short pause", "long pause"},
        "hold": {"short pause", "long pause"},
    }
    for beat, v3, sparse in zip(
        block["beats"], direction.beat_deliveries, result.beat_tags, strict=True
    ):
        text = beat["text"]
        if sparse.text != "":
            raise VoicePlanError("V4 must leave original beat text empty")
        expected: dict[tuple[str, int, int | None, int], set[str]] = {}
        cursor = 0
        for index, item in enumerate(v3.delivery):
            if isinstance(item, Speak):
                for intent in item.vocal_intents:
                    expected[("vocal_intent", index, intent.at, cursor + intent.at)] = {
                        voice_tags[intent.behavior]
                    }
                cursor += len(item.speak)
            else:
                expected[("pause", index, None, cursor)] = pause_tags[item.pause]
        if cursor != len(text):
            raise VoicePlanError("V4 original B2 and V3 beat text differ")
        observed: set[tuple[str, int, int | None, int]] = set()
        for event in sparse.inserts:
            key = (event.source_kind, event.source_item_index, event.source_at, event.at)
            if key not in expected or key in observed or event.tag not in expected[key]:
                raise VoicePlanError("V4 invented, duplicated or moved a V3 event")
            observed.add(key)
            if event.at > len(text) or (
                0 < event.at < len(text)
                and text[event.at - 1].isalnum()
                and text[event.at].isalnum()
            ):
                raise VoicePlanError("V4 event exceeds beat or splits word")
            if event.text != _minimal_tag_excerpt(text, event.at, event.tag):
                raise VoicePlanError("V4 excerpt must use only exact adjacent words")
            if event.source_kind == "pause":
                if not event.pause_direction or not event.pause_direction.strip():
                    raise VoicePlanError("V4 lost V3 explicit pause function")
            elif event.pause_direction is not None:
                raise VoicePlanError("V4 invented a pause purpose on human vocalization")
        # V4 has the final say on optional tag insertion. Every selected
        # tag still needs valid V3 provenance and an exact B2 source offset.
    if len(_v4_style(result, direction).encode("utf-8")) > 5500:
        raise VoicePlanError("V4 group-wide style exceeds Gemini budget")


def _tagged_v4(original_beat: str, sparse: V4BeatTags) -> str:
    """Insert ONLY validated tags into the canonical source beat."""
    rendered = original_beat
    for event in sorted(
        sparse.inserts, key=lambda item: (item.at, item.source_item_index),
        reverse=True,
    ):
        rendered = rendered[:event.at] + f"<{event.tag}>" + rendered[event.at:]
    return rendered


def _v4_turns(direction: V4, block: dict[str, Any], source: V3) -> list[dict[str, str]]:
    """One full transcript and ONE style for the entire voice group."""
    _check_v3(source, block)  # No bypass through saved or injected V3 results.
    text = ""
    for beat, sparse in zip(block["beats"], direction.beat_tags, strict=True):
        rendered = _tagged_v4(beat["text"], sparse)
        if text and not text[-1].isspace() and rendered and not rendered[0].isspace():
            text += " "
        text += rendered
    return [{"text": text, "style": _v4_style(direction, source)}]


async def _time_stage(
    stage: str, operation: Any, on_stage_duration: Callable[..., None] | None,
) -> Any:
    started = perf_counter()
    try:
        return await operation
    finally:
        if on_stage_duration is not None:
            on_stage_duration(stage, perf_counter() - started)


async def _direct(
    stage: str, payload: dict[str, Any], output_type: type[Strict], check: Callable[[Any], None],
    provider: Any, model: str, folder: Path, on_response: Callable[..., None] | None,
    on_timing: Callable[..., None] | None, block_id: int | None,
) -> Any:
    """Exactly two LOCAL validation correction retries, never paid-TTS retries."""
    prompt_path = files("ai_video_factory.bots.prompts").joinpath(
        stage.lower() + ".md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    cached = folder / "output.json"
    initial = folder / "input.json"
    if cached.exists() and initial.exists() and _read(initial).get("payload") == payload:
        try:
            prior = output_type.model_validate(_read(cached))
            check(prior)
            return prior
        except (ValueError, ValidationError) as exc:
            # A V3 checkpoint accepted by the former validator can contain
            # technical tags in artistic prose. Preserve it for diagnosis and
            # re-run ONLY that V3 block, then let V4 re-check its input.
            # Other invalid checkpoints and legacy paid V3 stay fail-closed.
            if (stage == "V3" and output_type is V3
                and isinstance(exc, VoicePlanError)
                and str(exc).startswith("V3 inline-tag leakage")):
                preserved = folder / "rejected_v3_inline_tags.json"
                if not preserved.exists():
                    _json(preserved, _read(cached))
            else:
                raise VoicePlanError(
                    f"{stage} block {block_id}: saved result no longer validates"
                ) from exc
    previous_id = None
    validation_feedback: dict[str, Any] | None = None
    for attempt in range(3):
        instructions = prompt
        data: dict[str, Any] = {"payload": payload}
        if validation_feedback is not None:
            data["validation_feedback"] = validation_feedback
        # One stable input checkpoint per V unit. A local validation retry
        # overwrites this file instead of creating attempt/rejection files.
        _json(folder / "input.json", data)
        start = perf_counter()
        try:
            response_obj, raw = await provider.generate_structured_with_response(
                model=model, instructions=instructions,
                input_text=json.dumps(data, ensure_ascii=False),
                output_type=output_type, previous_response_id=previous_id,
            )
        except ValidationError as exc:
            elapsed = perf_counter() - start
            if on_timing is not None:
                on_timing(stage, block_id, elapsed)
            validation_feedback = {
                "error": f"JSON schema validation: {exc}",
                "response": {"unparsed": "Provider parsed no valid output"},
                "task": (
                    "Correct only the stated contract violations; "
                    "all source strings and IDs are immutable."
                ),
            }
            if attempt == 2:
                raise VoicePlanError(
                    f"{stage} block {block_id}: provider schema validation exhausted"
                ) from exc
            continue
        except Exception as exc:
            elapsed = perf_counter() - start
            if on_timing is not None:
                on_timing(stage, block_id, elapsed)
            raise VoicePlanError(f"{stage} block {block_id}: provider failed: {exc}") from exc
        elapsed = perf_counter() - start
        if on_response:
            on_response(stage, block_id, raw)
        if on_timing:
            on_timing(stage, block_id, elapsed)
        previous_id = getattr(raw, "id", None)
        try:
            check(response_obj)
        except (ValueError, TypeError) as exc:
            v3_tag_leak = (
                stage == "V3" and output_type is V3
                and str(exc).startswith("V3 inline-tag leakage")
            )
            validation_feedback = {
                "error": str(exc),
                # Feeding literal rejected tokens back to V3 encourages it
                # to repeat the same markup. Keep those tokens in V4 only.
                "rejected_response": (
                    {"omitted": "Previous V3 output contained forbidden technical tags"}
                    if v3_tag_leak else response_obj.model_dump(
                        mode="json", by_alias=True,
                    )
                ),
                "task": (
                    "Recreate this V3 block using ONLY artistic performance "
                    "instructions and verbatim B2 text. No angle-bracket "
                    "tokens anywhere in V3; V4 is solely responsible for "
                    "their translation and insertion. Keep semantic "
                    "vocal_intents and functional pauses if justified."
                    if v3_tag_leak else
                    "Correct only the stated contract violations; "
                    "all source strings and IDs are immutable."
                ),
            }
            if attempt == 2:
                raise VoicePlanError(
                    f"{stage} block {block_id}: validation failed after 2 retries: {exc}"
                ) from exc
            continue
        _json(folder / "output.json", response_obj.model_dump(mode="json", by_alias=True))
        return response_obj
    raise AssertionError("Unreachable V retry state")


def _resolved(rhythm: BlockRhythm, beat: BeatIntent) -> dict[str, Any]:
    return {
        "beat_id": beat.beat_id,
        "function": beat.function,
        "understanding_delta": beat.understanding_delta,
        "viewer_trajectory": beat.viewer_trajectory,
        "tempo": _TEMPO[_TEMPO.index(rhythm.tempo) +
                         {"below": -1, "at": 0, "above": 1}[beat.tempo_modulation]],
        "pressure": _PRESSURE[_PRESSURE.index(rhythm.pressure) +
                             {"below": -1, "at": 0, "above": 1}[beat.pressure_modulation]],
    }


async def plan_professional_voice(
    *, core: dict[str, Any], blocks: list[dict[str, Any]], output: Path, provider: Any,
    model: str, on_response: Callable[..., None] | None = None,
    on_timing: Callable[..., None] | None = None,
    on_stage_duration: Callable[..., None] | None = None,
    max_parallel_calls: int = 8,
) -> tuple[V1, list[V2], list[V3], list[V4]]:
    """No paid speech request starts before all V contracts validate."""
    legacy = output / "voice_direction"
    if legacy.is_symlink() or any((output / stage).is_symlink()
                                 for stage in ("V1", "V2", "V3", "V4")):
        raise VoicePlanError("Linked voice checkpoint root")
    if legacy.exists():
        raise VoicePlanError(
            "Legacy voice checkpoints found; rerun from B2 to recreate V1-V4 "
            "with only input.json and output.json."
        )
    root = output
    voice_blocks = _voice_blocks_for_plan(blocks, root)
    # Legacy V3 had a second independent Gemini-tag compiler. Disable it;
    # protect existing paid WAVs by failing before any new model/API call.
    for block in voice_blocks:
        checkpoint = root / "V3" / f"block_{block['block_id']}" / "output.json"
        if not checkpoint.is_file():
            continue
        saved = _read(checkpoint)
        deliveries = saved.get("beat_deliveries")
        if not isinstance(deliveries, list) or not deliveries:
            raise VoicePlanError(f"Invalid saved V3 checkpoint: {checkpoint}")
        if any(isinstance(beat, dict) and "segments" in beat
               for beat in deliveries):
            raise VoicePlanError(
                "Legacy V3 checkpoint has obsolete Gemini tag generation; "
                f"preserved at {checkpoint}. Start a new professional voice "
                "run with modern V3→V4. Existing paid audio is not touched "
                "or automatically replayed."
            )
    contested: list[dict[str, Any]] = []
    v1: V1 | None = None
    v2s: list[V2] = []
    # V2 misfit re-enters the film-level V1; settled baselines stop re-entry.
    settled: set[tuple[int, str, str]] = set()
    for _iteration in range(9):
        v1 = await _time_stage(
            "V1",
            _direct("V1", {"narrative_core": core, "blocks": voice_blocks,
                           "contested_baselines": contested}, V1,
                    lambda value: _check_v1(value, voice_blocks, contested), provider, model,
                    root / "V1", on_response, on_timing, None),
            on_stage_duration,
        )
        rhythm_by_id = {r.block_id: r for r in v1.block_rhythms}
        semaphore = asyncio.Semaphore(max_parallel_calls)

        async def one(
            block: dict[str, Any],
            *, _semaphore: asyncio.Semaphore = semaphore,
            _rhythms: dict[int, BlockRhythm] = rhythm_by_id,
            _global: V1 = v1,
        ) -> V2:
            async with _semaphore:
                bid = block["block_id"]
                rhythm = _rhythms[bid]
                return await _direct(
                    "V2", {"narrative_core": core, "curve": _global.curve,
                           "block_rhythms": [x.model_dump() for x in _global.block_rhythms],
                           "current_block": block},
                    V2, lambda candidate: _check_v2(candidate, block, rhythm), provider, model,
                    root / "V2" / f"block_{bid}",
                    on_response, on_timing, bid,
                )

        v2_outcomes = await _time_stage(
            "V2",
            asyncio.gather(*(one(block) for block in voice_blocks), return_exceptions=True),
            on_stage_duration,
        )
        v2_failures = [entry for entry in v2_outcomes if isinstance(entry, BaseException)]
        if v2_failures:
            raise VoicePlanError(
                f"V2 has {len(v2_failures)} incomplete block(s); "
                "valid blocks were checkpointed for safe recovery"
            ) from v2_failures[0]
        v2s = [entry for entry in v2_outcomes if isinstance(entry, V2)]
        new: list[dict[str, Any]] = []
        for v2 in v2s:
            rhythm = rhythm_by_id[v2.block_id]
            for mismatch in v2.baseline_misfit:
                value = getattr(rhythm, mismatch.axis)
                key = (v2.block_id, mismatch.axis, value)
                if key not in settled:
                    new.append({
                        "block_id": v2.block_id, "axis": mismatch.axis,
                        "contested_value": value, "reading": mismatch.reading,
                        "beat_ids": mismatch.beat_ids,
                        "block_beat_count": len(next(block["beats"] for block in voice_blocks
                                                     if block["block_id"] == v2.block_id)),
                    })
        if not new:
            break
        # Same baseline re-emitted AFTER considering its misfit is settled.
        if contested:
            for prior in contested:
                rhythm = rhythm_by_id[prior["block_id"]]
                if getattr(rhythm, prior["axis"]) == prior["contested_value"]:
                    settled.add((prior["block_id"], prior["axis"], prior["contested_value"]))
        contested.extend(new)
    else:
        raise VoicePlanError("V1/V2 baseline reconciliation did not converge")
    if v1 is None:
        raise VoicePlanError("V1 failed to produce global rhythm")
    rhythms = {r.block_id: r for r in v1.block_rhythms}
    semaphore = asyncio.Semaphore(max_parallel_calls)

    async def perform(block: dict[str, Any], v2: V2) -> V3:
        async with semaphore:
            bid = block["block_id"]
            return await _direct(
                "V3", {"narrative_core": core, "curve": v1.curve, "current_block": block,
                       "block_rhythm": rhythms[bid].model_dump(),
                       "beat_intents": [_resolved(rhythms[bid], beat) for beat in v2.beat_intents],
                       "landing_beat_id": v2.landing_beat_id},
                V3, lambda value: _check_v3(value, block),
                provider, model, root / "V3" / f"block_{bid}",
                on_response, on_timing, bid,
            )

    v3_outcomes = await _time_stage(
        "V3",
        asyncio.gather(
            *(perform(block, v2) for block, v2 in zip(voice_blocks, v2s, strict=True)),
            return_exceptions=True,
        ),
        on_stage_duration,
    )
    v3_failures = [entry for entry in v3_outcomes if isinstance(entry, BaseException)]
    if v3_failures:
        raise VoicePlanError(
            f"V3 has {len(v3_failures)} incomplete block(s); "
            "valid blocks were checkpointed for safe recovery"
        ) from v3_failures[0]
    v3s = [entry for entry in v3_outcomes if isinstance(entry, V3)]
    v4_semaphore = asyncio.Semaphore(max_parallel_calls)

    async def compile_one(block: dict[str, Any], direction: V3) -> V4:
        async with v4_semaphore:
            bid = block["block_id"]
            return await _direct(
                "V4",
                {"current_block": block, "v3": direction.model_dump(
                    mode="json", by_alias=True,
                ),
                 "voice_contract": "professional-v4-group-style-sparse-tags-v2"},
                V4, lambda value: _check_v4(value, block, direction),
                provider, model, root / "V4" / f"block_{bid}",
                on_response, on_timing, bid,
            )

    v4_outcomes = await _time_stage(
        "V4",
        asyncio.gather(*(
            compile_one(block, direction)
            for block, direction in zip(voice_blocks, v3s, strict=True)
            if isinstance(direction, V3)
        ), return_exceptions=True),
        on_stage_duration,
    )
    v4_failures = [
        entry for entry in v4_outcomes if isinstance(entry, BaseException)
    ]
    if v4_failures:
        raise VoicePlanError(
            f"V4 has {len(v4_failures)} incomplete block(s); "
            "validated blocks were checkpointed for safe recovery"
        ) from v4_failures[0]
    v4s = [entry for entry in v4_outcomes if isinstance(entry, V4)]
    _json(root / "V4" / "merged_output.json", {
        "pipeline_stage": "V4",
        "blocks": [item.model_dump(mode="json", by_alias=True) for item in v4s],
    })
    # Present the final reconciled result like B1.2/B2, without hiding
    # intermediate passes and per-block files needed for troubleshooting.
    _json(root / "V1" / "merged_output.json", v1.model_dump(mode="json", by_alias=True))
    _json(root / "V2" / "merged_output.json", {
        "pipeline_stage": "V2",
        "blocks": [item.model_dump(mode="json") for item in v2s],
    })
    _json(root / "V3" / "merged_output.json", {
        "pipeline_stage": "V3",
        "blocks": [item.model_dump(mode="json", by_alias=True) for item in v3s],
    })
    return v1, v2s, v3s, v4s


def _reuse_verified_block(
    *, output: Path, current_run: Path, block_id: int, fingerprint: str,
    model: str, voice_id: str, folder: Path,
) -> str | None:
    """Reuse an identical paid Gemini WAV across corrected B2 global plans.

    Both the original run and block state must explicitly say the paid request
    completed, and its raw bytes must match the saved SHA. A pending/unknown
    request never enters this cache. Only immutable audio and optional valid STT
    transcripts are copied; each destination still owns its own task identity.
    """
    for old_run in sorted((output / "audio" / "runs").glob("gemini-*")):
        if old_run == current_run or old_run.is_symlink():
            continue
        root_path = old_run / "task.json"
        prior_path = old_run / "blocks" / f"block_{block_id}" / "task.json"
        if not root_path.is_file() or not prior_path.is_file():
            continue
        if root_path.is_symlink() or prior_path.is_symlink():
            continue
        root = _read(root_path)
        if root.get("voice") != voice_id or root.get("model") != model:
            continue
        prior = _read(prior_path)
        if prior.get("fingerprint") != fingerprint or prior.get("status") not in {
            "tts_completed", "completed",
        }:
            continue
        digest = prior.get("sha256")
        source = prior_path.parent / "audio.wav"
        if (not isinstance(digest, str) or source.is_symlink()
            or not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != digest):
            continue
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / "audio.wav"
        if destination.is_symlink():
            raise VoicePlanError(f"Linked destination audio path: {destination}")
        shutil.copyfile(source, destination)
        # Reuse exactly matching STT; otherwise _transcribe_block owns the new call.
        previous_stt = prior_path.parent / "stt.json"
        if previous_stt.is_file() and not previous_stt.is_symlink():
            transcript = _read(previous_stt)
            if (transcript.get("source_audio_sha256") == digest
                and transcript.get("model") == "whisper-1"
                and isinstance(transcript.get("words"), list) and transcript["words"]):
                target = folder / "stt.json"
                if target.is_symlink():
                    raise VoicePlanError(f"Linked destination STT path: {target}")
                shutil.copyfile(previous_stt, target)
        return digest
    return None


async def generate_professional_audio(
    *, core: dict[str, Any], blocks: list[dict[str, Any]], output: Path,
    provider: Any, model: str, gemini_api_key: str, voice_id: str,
    on_response: Callable[..., None] | None = None,
    on_timing: Callable[..., None] | None = None,
    on_stage_duration: Callable[..., None] | None = None,
    max_parallel_calls: int = 8, stt_client: OpenAISttClient | None = None,
    speech_provider: GeminiTtsSpeechProvider | None = None,
    generation_nonce: str = "",
    source_sha256: str = "",
) -> dict[str, Any]:
    """Publish professional block WAVs and an existing-timeline-compatible artifact."""
    if not blocks or not gemini_api_key:
        raise VoicePlanError("Professional Gemini voice requires B2 blocks and GEMINI_API_KEY")
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise VoicePlanError("Professional Gemini voice ID is required")
    v1, v2s, v3s, v4s = await plan_professional_voice(
        core=core, blocks=blocks, output=output, provider=provider, model=model,
        on_response=on_response, on_timing=on_timing,
        on_stage_duration=on_stage_duration,
        max_parallel_calls=max_parallel_calls,
    )
    voice_blocks = _voice_blocks_for_audio(blocks, output)
    groups = [
        {"block_id": block["block_id"],
         "source_block_ids": block.get("source_block_ids", [block["block_id"]]),
         "beat_ids": [beat["beat_id"] for beat in block["beats"]]}
        for block in voice_blocks
    ]
    grouped_plan = any(
        block.get("source_block_ids", [block["block_id"]]) != [block["block_id"]]
        for block in voice_blocks
    )
    if len(v4s) != len(voice_blocks):
        raise VoicePlanError("Every new professional voice block requires validated V4")
    # Fingerprint includes effective directions and the compiler contract.
    payload = {"blocks": blocks, "v1": v1.model_dump(by_alias=True),
               "v2": [x.model_dump(by_alias=True) for x in v2s],
               "v3": [x.model_dump(by_alias=True) for x in v3s], "voice": voice_id,
               "model": DEFAULT_GEMINI_TTS_MODEL, "generation_nonce": generation_nonce,
               "source_sha256": source_sha256}
    payload["v4"] = [x.model_dump(by_alias=True) for x in v4s]
    payload["compiler_version"] = "professional-v4-exclusive-tags-v3"
    if grouped_plan:
        payload["voice_groups"] = groups
    fingerprint = _fingerprint(payload)
    run_dir = output / "audio" / "runs" / ("gemini-" + fingerprint[:24])
    manifest = run_dir / "task.json"
    if manifest.exists():
        task = _read(manifest)
        if (task.get("fingerprint") != fingerprint
            or task.get("source_sha256") != source_sha256):

            raise VoicePlanError("Professional audio fingerprint collision")
        if task.get("status") == "completed":
            artifact = task.get("audio")
            if not isinstance(artifact, dict):
                raise VoicePlanError("Completed Gemini audio has no artifact")
            for item in artifact.get("blocks", []):
                source = output / item["file"]
                if source.is_symlink() or not source.is_file() or (
                    hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]
                ):
                    raise VoicePlanError("Cached professional audio differs from saved SHA-256")
            _json(output / "audio" / "latest.json", artifact)
            return artifact
    else:
        _json(manifest, {"fingerprint": fingerprint, "status": "planned",
                         "model": DEFAULT_GEMINI_TTS_MODEL, "voice": voice_id,
                         "run_id": run_dir.name, "source_sha256": source_sha256,
                         "generation_nonce": generation_nonce,
                         "block_ids": [b["block_id"] for b in voice_blocks]})
    speech = speech_provider or GeminiTtsSpeechProvider(api_key=gemini_api_key)
    stt = stt_client or OpenAISttClient(api_key=settings.openai_api_key)
    semaphore = asyncio.Semaphore(max_parallel_calls)

    async def one(
        block: dict[str, Any], direction: V3, compiler: V4,
    ) -> dict[str, Any]:
        bid = block["block_id"]
        source_ids = block.get("source_block_ids", [bid])
        folder = run_dir / "blocks" / f"block_{bid}"
        state_path = folder / "task.json"
        async with semaphore:
            turns = _v4_turns(compiler, block, direction)
            speech_fingerprint = _fingerprint({
                "turns": turns, "source_block_ids": source_ids,
                "compiler_version": "professional-v4-exclusive-tags-v3",
            })
            if state_path.exists():
                state = _read(state_path)
                if state.get("fingerprint") != speech_fingerprint:
                    raise VoicePlanError(f"Gemini block {bid} saved direction differs")
            else:
                state = {"block_id": bid, "fingerprint": speech_fingerprint, "status": "planned"}
                _json(state_path, state)
            audio_path = folder / "audio.wav"
            # A block is either completed or retryable.  Older runs may still
            # contain the removed ambiguous states; migrate them to planned so
            # --resume-audio can finish the run after a provider failure.
            if state["status"] in {
                "tts_submitting_unknown", "tts_invalid", "tts_failed",
            }:
                state["status"] = "planned"
                state.pop("error", None)
                _json(state_path, state)
            if state["status"] == "planned":
                # Explicit --regenerate-audio uses a new nonce and must generate
                # fresh audio; normal B2 correction may reuse identical speech.
                reused_digest = (
                    _reuse_verified_block(
                        output=output, current_run=run_dir, block_id=bid,
                        fingerprint=speech_fingerprint, model=DEFAULT_GEMINI_TTS_MODEL,
                        voice_id=voice_id, folder=folder,
                    )
                    if not generation_nonce else None
                )
                if reused_digest is not None:
                    state["status"] = "tts_completed"
                    state["sha256"] = reused_digest
                    _json(state_path, state)
            if state["status"] == "planned":
                # Keep an incomplete block retryable until a verified WAV is
                # available.  There is deliberately no ambiguous submission
                # state in this workflow.
                try:
                    generated = await speech.generate_directed_speech(
                        turns=turns, model=DEFAULT_GEMINI_TTS_MODEL, voice=voice_id,
                    )
                except Exception as exc:
                    # Failed blocks are retryable.  The caller explicitly opted
                    # into retrying incomplete audio instead of preserving an
                    # ambiguous state that permanently blocks the run.
                    state["status"] = "tts_failed"
                    state["error"] = type(exc).__name__
                    _json(state_path, state)
                    raise
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                audio_path.write_bytes(generated.content)
                state["status"] = "tts_completed"
                state["sha256"] = hashlib.sha256(generated.content).hexdigest()
                _json(state_path, state)
            if (audio_path.is_symlink() or not audio_path.is_file()
                or hashlib.sha256(audio_path.read_bytes()).hexdigest() != state.get("sha256")):
                raise VoicePlanError(f"Gemini block {bid}: saved WAV unavailable or changed")
            item: dict[str, Any] = {
                "block_id": bid, "source_block_ids": source_ids,
                "task_id": f"gemini-{fingerprint[:12]}-{bid}",
                "file": audio_path.relative_to(output).as_posix(),
                "sha256": state["sha256"], "format": "wav", "bytes": audio_path.stat().st_size,
                "credit_cost": None, "director_usage": None,
            }
            item = await _transcribe_block(item, output=output, run_dir=run_dir, stt_client=stt)
            state["status"] = "completed"
            _json(state_path, state)
            return item

    results = await asyncio.gather(
        *(one(block, v3, v4s[index])
          for index, (block, v3) in enumerate(zip(voice_blocks, v3s, strict=True))),
        return_exceptions=True,
    )
    failures = [x for x in results if isinstance(x, BaseException)]
    if failures:
        _json(manifest, {"fingerprint": fingerprint, "status": "incomplete",
                         "run_id": run_dir.name, "source_sha256": source_sha256,
                         "model": DEFAULT_GEMINI_TTS_MODEL, "voice": voice_id,
                         "generation_nonce": generation_nonce,
                         "error": (
                             f"{len(failures)} block(s) incomplete; inspect block task states"
                         )})
        raise VoicePlanError(
            f"Professional Gemini audio has {len(failures)} incomplete blocks; "
            f"inspect {manifest}"
        ) from failures[0]
    items = [x for x in results if isinstance(x, dict)]
    joined = await asyncio.to_thread(
        join_audio_blocks, [output / x["file"] for x in items], run_dir / "narration.wav",
    )
    artifact = {
        "status": "completed", "provider": "gemini", "voice_source": "gemini",
        "voice_id": voice_id, "run_id": run_dir.name,
        "file": (run_dir / "narration.wav").relative_to(output).as_posix(),
        "sha256": joined["sha256"], "format": "wav", "bytes": joined["bytes"],
        "duration_seconds": joined["duration_seconds"],
        "pause_between_blocks_seconds": 0.5,
        "block_start_seconds": joined["block_start_seconds"],
        "block_end_seconds": joined["block_end_seconds"],
        "block_durations_seconds": joined["block_durations_seconds"],
        "stt_complete": True, "stt_model": stt.model, "block_count": len(items),
        **({"block_groups": groups} if grouped_plan else {}),
        "blocks": items, "credit_cost": None, "director_model": model,
    }
    _json(manifest, {"fingerprint": fingerprint, "status": "completed",
                     "run_id": run_dir.name, "source_sha256": source_sha256,
                     "model": DEFAULT_GEMINI_TTS_MODEL, "voice": voice_id,
                     "generation_nonce": generation_nonce, "audio": artifact})
    _json(output / "audio" / "latest.json", artifact)
    return artifact
