"""Professional post-B2 V1/V2/V3 direction and block-local Gemini 3.8 speech.

B2 is the source of visual context, never a source of extra spoken words.
All paid operations have durable state; an ambiguous Gemini POST is NEVER replayed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_video_factory.bots.audio_join import join_audio_blocks
from ai_video_factory.bots.fish_audio_workflow import _transcribe_block
from ai_video_factory.config import settings
from ai_video_factory.providers.gemini_tts import (
    DEFAULT_GEMINI_TTS_MODEL, GeminiTtsSpeechProvider,
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


class VocalEvent(Strict):
    at: int = Field(ge=0)
    tag: Tag


class Speak(Strict):
    speak: str = Field(min_length=1)
    style: str = Field(min_length=1, max_length=360)
    ending_behavior: Ending
    events: list[VocalEvent]


class BeatDelivery(Strict):
    beat_id: str
    segments: list[Speak] = Field(min_length=1)


class V3(Strict):
    pipeline_stage: Literal["V3"]
    block_id: int
    beat_deliveries: list[BeatDelivery]


class VoicePlanError(ValueError):
    """A V stage returned semantically invalid or unsafe direction."""


_TEMPO = ("held", "measured", "driving", "relentless")
_PRESSURE = ("low", "moderate", "high", "extreme")
_ALLOWED_TAGS = frozenset({"breath", "exhales", "sigh", "chuckle", "gasp",
                           "short pause", "long pause"})


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
        raise VoicePlanError(f"V2 block {block['block_id']}: beat_ids must exactly match {beat_ids}")
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
    beats = block["beats"]
    if result.block_id != block["block_id"] or [b.beat_id for b in result.beat_deliveries] != [
        b["beat_id"] for b in beats
    ]:
        raise VoicePlanError(f"V3 block {block['block_id']} changed beat identity or order")
    for incoming, delivered in zip(beats, result.beat_deliveries, strict=True):
        if "".join(piece.speak for piece in delivered.segments) != incoming["text"]:
            raise VoicePlanError(f"V3 altered verbatim narration in beat {incoming['beat_id']}")
        if re.search(r"<[^>]+>", incoming["text"]):
            raise VoicePlanError(f"V3 source beat {incoming['beat_id']} has ambiguous literal tags")
        count = 0
        for piece in delivered.segments:
            if len(piece.events) > 2:
                raise VoicePlanError(f"V3 too many events in fragment of {incoming['beat_id']}")
            for event in piece.events:
                count += 1
                if event.tag not in _ALLOWED_TAGS or event.at > len(piece.speak):
                    raise VoicePlanError(f"V3 unsafe event in {incoming['beat_id']}")
                if (0 < event.at < len(piece.speak)
                    and piece.speak[event.at - 1].isalnum() and piece.speak[event.at].isalnum()):
                    raise VoicePlanError(f"V3 event splits a word in {incoming['beat_id']}")
            if len({event.at for event in piece.events}) != len(piece.events):
                raise VoicePlanError(f"V3 coinciding events in {incoming['beat_id']}")
        if count > 2:
            raise VoicePlanError(f"V3 beat {incoming['beat_id']} exceeds human-event safety limit")


def _tagged(piece: Speak) -> str:
    text = piece.speak
    for event in sorted(piece.events, key=lambda x: x.at, reverse=True):
        text = text[:event.at] + f"<{event.tag}>" + text[event.at:]
    return text


async def _direct(
    stage: str, payload: dict[str, Any], output_type: type[Strict], check: Callable[[Any], None],
    provider: Any, model: str, folder: Path, on_response: Callable[..., None] | None,
    on_timing: Callable[..., None] | None, block_id: int | None,
) -> Any:
    """Exactly two LOCAL validation correction retries, never paid-TTS retries."""
    prompt = files("ai_video_factory.bots.prompts").joinpath(stage.lower() + ".md").read_text(
        encoding="utf-8"
    )
    cached = folder / "output.json"
    initial = folder / "input.json"
    if cached.exists() and initial.exists() and _read(initial).get("payload") == payload:
        try:
            prior = output_type.model_validate(_read(cached))
            check(prior)
            return prior
        except (ValueError, ValidationError):
            # A corrupt saved result cannot silently be used as model authority.
            raise VoicePlanError(f"{stage} block {block_id}: saved result no longer validates")
    previous_id = None
    for attempt in range(3):
        instructions = prompt
        data: dict[str, Any] = {"payload": payload}
        if attempt:
            rejected = _read(folder / f"rejected_attempt_{attempt}.json")
            data["validation_feedback"] = {
                "error": rejected["error"], "rejected_response": rejected["response"],
                "task": "Correct only the stated contract violations; all source strings and IDs are immutable.",
            }
        _json(folder / ("input.json" if attempt == 0 else f"input_attempt_{attempt + 1}.json"), data)
        start = perf_counter()
        try:
            response_obj, raw = await provider.generate_structured_with_response(
                model=model, instructions=instructions,
                input_text=json.dumps(data, ensure_ascii=False),
                output_type=output_type, previous_response_id=previous_id,
            )
        except ValidationError as exc:
            # A provider response reached schema validation but could not be parsed.
            # Give the text bot a clear correction message; no paid speech is involved.
            _json(folder / f"rejected_attempt_{attempt + 1}.json", {
                "attempt": attempt + 1, "error": f"JSON schema validation: {exc}",
                "response": {"unparsed": "Provider parsed no valid output"},
            })
            if attempt == 2:
                raise VoicePlanError(
                    f"{stage} block {block_id}: provider schema validation exhausted"
                ) from exc
            continue
        except Exception as exc:
            # Transport or unknown POST outcomes must not silently be replayed.
            raise VoicePlanError(f"{stage} block {block_id}: provider failed: {exc}") from exc
        if on_response:
            on_response(stage, block_id, raw)
        if on_timing:
            on_timing(stage, block_id, perf_counter() - start)
        previous_id = getattr(raw, "id", None)
        try:
            check(response_obj)
        except (ValueError, TypeError) as exc:
            _json(folder / f"rejected_attempt_{attempt + 1}.json", {
                "attempt": attempt + 1, "error": str(exc),
                "response": response_obj.model_dump(mode="json"),
            })
            if attempt == 2:
                raise VoicePlanError(f"{stage} block {block_id}: validation failed after 2 retries: {exc}") from exc
            continue
        _json(folder / "output.json", response_obj.model_dump(mode="json"))
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
    on_timing: Callable[..., None] | None = None, max_parallel_calls: int = 8,
) -> tuple[V1, list[V2], list[V3]]:
    """No paid speech request starts before all V contracts validate."""
    root = output / "voice_direction"
    contested: list[dict[str, Any]] = []
    v1: V1 | None = None
    v2s: list[V2] = []
    # V2 misfit re-enters the film-level V1; settled baselines stop re-entry.
    settled: set[tuple[int, str, str]] = set()
    for iteration in range(9):
        v1 = await _direct("V1", {"narrative_core": core, "blocks": blocks,
                               "contested_baselines": contested}, V1,
                           lambda value: _check_v1(value, blocks, contested), provider, model,
                           root / "V1" / f"pass_{iteration + 1}", on_response, on_timing, None)
        rhythm_by_id = {r.block_id: r for r in v1.block_rhythms}
        semaphore = asyncio.Semaphore(max_parallel_calls)

        async def one(block: dict[str, Any]) -> V2:
            async with semaphore:
                bid = block["block_id"]
                rhythm = rhythm_by_id[bid]
                return await _direct(
                    "V2", {"narrative_core": core, "curve": v1.curve,
                           "block_rhythms": [x.model_dump() for x in v1.block_rhythms],
                           "current_block": block},
                    V2, lambda value: _check_v2(value, block, rhythm), provider, model,
                    root / "V2" / f"pass_{iteration + 1}" / f"block_{bid}",
                    on_response, on_timing, bid,
                )

        v2s = list(await asyncio.gather(*(one(block) for block in blocks)))
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
                        "block_beat_count": len(next(block["beats"] for block in blocks
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
    assert v1 is not None
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
                V3, lambda value: _check_v3(value, block), provider, model,
                root / "V3" / f"block_{bid}", on_response, on_timing, bid,
            )

    v3s = list(await asyncio.gather(*(perform(block, v2)
                                      for block, v2 in zip(blocks, v2s, strict=True))))
    return v1, v2s, v3s


async def generate_professional_audio(
    *, core: dict[str, Any], blocks: list[dict[str, Any]], output: Path,
    provider: Any, model: str, gemini_api_key: str, voice_id: str,
    on_response: Callable[..., None] | None = None,
    on_timing: Callable[..., None] | None = None,
    max_parallel_calls: int = 8, stt_client: OpenAISttClient | None = None,
    speech_provider: GeminiTtsSpeechProvider | None = None,
    generation_nonce: str = "",
) -> dict[str, Any]:
    """Publish professional block WAVs and an existing-timeline-compatible artifact."""
    if not blocks or not gemini_api_key:
        raise VoicePlanError("Professional Gemini voice requires B2 blocks and GEMINI_API_KEY")
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise VoicePlanError("Professional Gemini voice ID is required")
    v1, v2s, v3s = await plan_professional_voice(
        core=core, blocks=blocks, output=output, provider=provider, model=model,
        on_response=on_response, on_timing=on_timing,
        max_parallel_calls=max_parallel_calls,
    )
    # Fingerprint includes effective directions; changed B2/V3 can never reuse paid audio.
    payload = {"blocks": blocks, "v1": v1.model_dump(), "v2": [x.model_dump() for x in v2s],
               "v3": [x.model_dump() for x in v3s], "voice": voice_id,
               "model": DEFAULT_GEMINI_TTS_MODEL, "generation_nonce": generation_nonce}
    fingerprint = _fingerprint(payload)
    run_dir = output / "audio" / "runs" / ("gemini-" + fingerprint[:24])
    manifest = run_dir / "task.json"
    if manifest.exists():
        task = _read(manifest)
        if task.get("fingerprint") != fingerprint:
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
                         "generation_nonce": generation_nonce, "block_ids": [b["block_id"] for b in blocks]})
    speech = speech_provider or GeminiTtsSpeechProvider(api_key=gemini_api_key)
    stt = stt_client or OpenAISttClient(api_key=settings.openai_api_key)
    semaphore = asyncio.Semaphore(max_parallel_calls)

    async def one(block: dict[str, Any], direction: V3) -> dict[str, Any]:
        bid = block["block_id"]
        folder = run_dir / "blocks" / f"block_{bid}"
        state_path = folder / "task.json"
        async with semaphore:
            turns = [{"text": _tagged(segment), "style": segment.style}
                     for delivery in direction.beat_deliveries for segment in delivery.segments]
            if state_path.exists():
                state = _read(state_path)
                if state.get("fingerprint") != _fingerprint(turns):
                    raise VoicePlanError(f"Gemini block {bid} saved direction differs")
            else:
                state = {"block_id": bid, "fingerprint": _fingerprint(turns), "status": "planned"}
                _json(state_path, state)
            audio_path = folder / "audio.wav"
            if state["status"] in {"tts_submitting_unknown", "tts_invalid"}:
                raise VoicePlanError(f"Gemini block {bid} may have incurred charges; reconcile {state_path}")
            if state["status"] == "planned":
                state["status"] = "tts_submitting_unknown"
                _json(state_path, state)
                try:
                    generated = await speech.generate_directed_speech(
                        turns=turns, model=DEFAULT_GEMINI_TTS_MODEL, voice=voice_id,
                    )
                except Exception:
                    # Keep ambiguous state even for malformed returned audio: paid call exists.
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
                "block_id": bid, "task_id": f"gemini-{fingerprint[:12]}-{bid}",
                "file": audio_path.relative_to(output).as_posix(),
                "sha256": state["sha256"], "format": "wav", "bytes": audio_path.stat().st_size,
                "credit_cost": None, "director_usage": None,
            }
            item = await _transcribe_block(item, output=output, run_dir=run_dir, stt_client=stt)
            state["status"] = "completed"
            _json(state_path, state)
            return item

    results = await asyncio.gather(
        *(one(block, v3) for block, v3 in zip(blocks, v3s, strict=True)),
        return_exceptions=True,
    )
    failures = [x for x in results if isinstance(x, BaseException)]
    if failures:
        _json(manifest, {"fingerprint": fingerprint, "status": "incomplete",
                         "error": f"{len(failures)} block(s) incomplete; inspect block task states"})
        raise VoicePlanError(f"Professional Gemini audio has {len(failures)} incomplete blocks; inspect {manifest}") from failures[0]
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
        "blocks": items, "credit_cost": None, "director_model": model,
    }
    _json(manifest, {"fingerprint": fingerprint, "status": "completed", "audio": artifact})
    _json(output / "audio" / "latest.json", artifact)
    return artifact
