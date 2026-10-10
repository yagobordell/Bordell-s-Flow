"""Post-B3, image-aware LTX motion adaptation; never edits the saved visual plan.

A call sees a chronological context window and the actual finalized start PNGs.
Cache keys bind its result to those PNGs, the effective LTX prompts and the bot
instructions. Only validated beat_id/video_prompt pairs can override a paid job.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ai_video_factory.config import settings
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.providers.openai import OpenAIProvider

_SPEAKING_PREFIX = "A Person speaking to the camera."
_MAX_ELIGIBLE_PER_GROUP = 10
_MAX_CONTEXT_IMAGES = 6


class B22MotionError(RuntimeError):
    """An image-aware motion decision could not be verified safely."""


class B22Beat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: str = Field(min_length=2)
    video_prompt: str = Field(min_length=1, max_length=4000)


class B22Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beats: list[B22Beat]


@dataclass(frozen=True)
class MotionShot:
    beat_id: str
    beat_ids: tuple[str, ...]
    kind: str
    image: Path
    image_sha256: str
    prompt: str
    motion_source: str
    duration: float
    eligible: bool
    start_visual_type: str


def _effective_motion(job: Any) -> str:
    prompt = job.request.parameters["prompt"]
    if job.kind == "a2v":
        if not prompt.startswith(_SPEAKING_PREFIX):
            raise B22MotionError("Unexpected A2V positive speaking prompt")
        return prompt[len(_SPEAKING_PREFIX):].strip()
    return prompt.strip()


def _shots(jobs: list[Any], timeline: dict[str, Any]) -> list[MotionShot]:
    beats = {beat["beat_id"]: beat for beat in timeline["beats"]}
    shots: list[MotionShot] = []
    for job in jobs:
        first = beats[job.beats[0]]
        motion = _effective_motion(job)
        source = (
            "b3_shot_video_prompt"
            if isinstance(first.get("b3_shot_video_prompt"), str)
            and first["b3_shot_video_prompt"].strip()
            else "video_prompt"
            if isinstance(first.get("video_prompt"), str)
            and first["video_prompt"].strip()
            else "none"
        )
        eligible = (
            bool(motion)
            and first["visual_type"] == "media_video"
            and job.kind == "i2v"
        ) or (
            bool(motion)
            and job.kind == "a2v"
            and first["visual_type"] != "avatar"
        )
        shots.append(MotionShot(
            beat_id=job.beats[0],
            beat_ids=job.beats,
            kind=job.kind,
            image=job.image,
            image_sha256=sha256_file(job.image),
            prompt=motion,
            motion_source=source,
            duration=job.duration_seconds,
            eligible=eligible,
            start_visual_type=first["visual_type"],
        ))
    return shots


def _safe_cache(cache_dir: Path, digest: str) -> Path:
    # Refuse linked cache locations; files cannot escape the run directory.
    for folder in (cache_dir, cache_dir.parent, cache_dir.parent.parent):
        if folder.is_symlink():
            raise B22MotionError("Refusing symlinked B2.2 cache directory")
    cache_dir.mkdir(parents=True, exist_ok=True)
    result = cache_dir / f"{digest}.json"
    if result.is_symlink():
        raise B22MotionError("Refusing symlinked B2.2 cache file")
    return result


def _save_cache(path: Path, result: B22Result) -> None:
    temp = path.with_name(f"{path.name}.{uuid4().hex}.part")
    try:
        temp.write_text(
            result.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _save_diagnostic_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic local metrics write, excluding prompts and image contents."""
    temp = path.with_name(f"{path.name}.{uuid4().hex}.part")
    try:
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _response_usage(raw: object, model: str) -> dict[str, Any]:
    """Read only bounded, billable metadata; never persist provider payloads."""
    def get(item: object, name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    usage = get(raw, "usage")
    details = get(usage, "input_tokens_details")
    out_details = get(usage, "output_tokens_details")

    def tokens(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    return {
        "id": get(raw, "id") if isinstance(get(raw, "id"), str) else None,
        "model": get(raw, "model") if isinstance(get(raw, "model"), str) else model,
        "service_tier": get(raw, "service_tier") or "default",
        "usage": {
            "input_tokens": tokens(get(usage, "input_tokens")),
            "output_tokens": tokens(get(usage, "output_tokens")),
            "input_tokens_details": {
                "cached_tokens": tokens(get(details, "cached_tokens")),
                "cache_write_tokens": tokens(get(details, "cache_write_tokens")),
            },
            "output_tokens_details": {
                "reasoning_tokens": tokens(get(out_details, "reasoning_tokens")),
            },
        },
    }


def _validate(result: B22Result, expected: list[str]) -> dict[str, str]:
    ids = [beat.beat_id for beat in result.beats]
    if ids != expected:
        raise B22MotionError(
            f"B2.2 beat IDs/order differ: expected {expected!r}, got {ids!r}"
        )
    for beat in result.beats:
        if not beat.video_prompt.strip():
            raise B22MotionError(f"B2.2 returned empty motion for {beat.beat_id}")
    return {beat.beat_id: beat.video_prompt.strip() for beat in result.beats}


def _chronology(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            name: beat.get(name)
            for name in (
                "block_id", "beat_id", "text", "beat_type", "visual_type",
                "audio", "continuous", "b3_group_id", "grid_id",
                "start_seconds", "end_seconds",
            )
            if name in beat
        }
        for beat in timeline["beats"]
    ]


async def _call_group(
    *,
    output: Path,
    shots: list[MotionShot],
    selected: list[MotionShot],
    timeline: dict[str, Any],
    context_images: dict[tuple[int, str], Path],
    model: str,
    provider: Any,
    instructions: str,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, str]:
    by_id = {beat["beat_id"]: beat for beat in timeline["beats"]}
    indices = [shots.index(shot) for shot in selected]
    lo, hi = min(indices), max(indices)
    # Context shots before/after the adapted ones help reserve future actions.
    around = shots[max(0, lo - 1):min(len(shots), hi + 2)]
    image_parts: list[tuple[str, Path]] = []
    seen_paths: set[Path] = set()

    def include(path: Path) -> str:
        if path not in seen_paths:
            seen_paths.add(path)
            image_parts.append((f"image_{len(image_parts) + 1}", path))
        return next(name for name, current in image_parts if current == path)

    shot_manifest: list[dict[str, Any]] = []
    for shot in around:
        first = by_id[shot.beat_id]
        image_id = include(shot.image)
        shot_manifest.append({
            "shot_id": f"shot_{shots.index(shot) + 1}",
            "output_beat_id": shot.beat_id,
            "beat_ids": list(shot.beat_ids),
            "kind": shot.kind,
            "start_beat_id": shot.beat_id,
            "start_visual_type": shot.start_visual_type,
            "duration_seconds": shot.duration,
            "start_image_id": image_id,
            "effective_motion_prompt": shot.prompt,
            "motion_source": shot.motion_source,
            "b2_video_prompt": first.get("video_prompt"),
            "b3_shot_video_prompt": first.get("b3_shot_video_prompt"),
            "eligible_for_b22": shot in selected,
            "speaking_setup": (
                "Frozen B3 speaker setup; fixed camera"
                if shot.kind == "a2v" else None
            ),
        })

    # Still beats between video shots can constrain prop state even when they do
    # not generate a paid LTX job. Limit supporting image tokens deterministically.
    timeline_beats = timeline["beats"]
    first_idx = next(
        i for i, beat in enumerate(timeline_beats)
        if beat["beat_id"] == around[0].beat_id
    )
    last_beat = around[-1].beat_ids[-1]
    last_idx = next(i for i, beat in enumerate(timeline_beats) if beat["beat_id"] == last_beat)
    # Include neighboring static images even when there is no later video job.
    first_idx = max(0, first_idx - 2)
    last_idx = min(len(timeline_beats) - 1, last_idx + 2)
    context_image_manifest: list[dict[str, str]] = []
    for beat in timeline_beats[first_idx:last_idx + 1]:
        if len(context_image_manifest) >= _MAX_CONTEXT_IMAGES:
            break
        if beat["visual_type"] not in {"media_image", "avatar_media"}:
            continue
        key = (beat["block_id"], beat["beat_id"])
        path = context_images.get(key)
        if path is not None:
            context_image_manifest.append({
                "beat_id": beat["beat_id"], "image_id": include(path),
            })

    # Place images on the wire in true beat chronology, including stills
    # between two video shots; retain the explicit image_id bindings.
    beat_positions = {
        beat["beat_id"]: index for index, beat in enumerate(timeline_beats)
    }
    image_position: dict[str, int] = {}
    for shot in shot_manifest:
        identifier = shot["start_image_id"]
        image_position[identifier] = min(
            image_position.get(identifier, len(timeline_beats)),
            beat_positions[shot["start_beat_id"]],
        )
    for still in context_image_manifest:
        identifier = still["image_id"]
        image_position[identifier] = min(
            image_position.get(identifier, len(timeline_beats)),
            beat_positions[still["beat_id"]],
        )
    image_parts.sort(key=lambda item: image_position[item[0]])
    renames = {
        previous: f"image_{index + 1}"
        for index, (previous, _path) in enumerate(image_parts)
    }
    image_parts = [(renames[name], path) for name, path in image_parts]
    for shot in shot_manifest:
        shot["start_image_id"] = renames[shot["start_image_id"]]
    for still in context_image_manifest:
        still["image_id"] = renames[still["image_id"]]

    manifest = {
        "group_id": f"{selected[0].beat_id}-{selected[-1].beat_id}",
        "group_context": (
            "All timeline beats are ordered and authoritative; preserve future events."
        ),
        "timeline": _chronology(timeline),
        "shots": shot_manifest,
        "context_images": context_image_manifest,
        "images": [
            {"image_id": name, "sha256": sha256_file(path)}
            for name, path in image_parts
        ],
        "expected_output_beat_ids": [shot.beat_id for shot in selected],
    }
    # Cache includes every contextual beat, image and instruction that may affect
    # the response; upgrades do not silently reuse old LTX prompt decisions.
    signature = json.dumps({
        "model": model,
        "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
        "manifest": manifest,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(signature.encode()).hexdigest()
    cached = _safe_cache(output / "video" / "b22" / "groups", digest)
    metrics_path = _safe_cache(output / "video" / "b22" / "usage", digest)
    expected = manifest["expected_output_beat_ids"]
    if cached.exists():
        restored = B22Result.model_validate_json(cached.read_text(encoding="utf-8"))
        validated = _validate(restored, expected)
        if on_event:
            on_event({"cache_hit": True, "adapted": len(validated),
                      "attempts": 0, "group_id": manifest["group_id"]})
        return validated

    urls = [
        "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        for _name, path in image_parts
    ]
    input_text = (
        "The following image parts are attached IN THE EXACT ORDER of the "
        "images array. Each image_id maps to its corresponding input_image part. "
        "Inspect all images before composing any motion prompt.\n"
        + json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    )
    last_error: B22MotionError | None = None
    recorded = []
    if metrics_path.exists():
        previous = json.loads(metrics_path.read_text(encoding="utf-8"))
        if isinstance(previous, dict) and isinstance(previous.get("attempts"), list):
            recorded = list(previous["attempts"])
    for attempt in range(2):
        response, _raw = await provider.generate_structured_with_response(
            model=model,
            instructions=instructions + (
                "\n\nCorrection: return only the exact expected beat IDs in the "
                "given order and preserve the two-field output contract."
                if attempt else ""
            ),
            input_text=input_text,
            input_image_urls=urls,
            output_type=B22Result,
        )
        recorded.append(_response_usage(_raw, model))
        _save_diagnostic_json(metrics_path, {
            "schema_version": 1, "attempts": recorded,
        })
        try:
            validated = _validate(response, expected)
        except B22MotionError as exc:
            last_error = exc
            continue
        _save_cache(cached, response)
        if on_event:
            on_event({"cache_hit": False, "adapted": len(validated),
                      "attempts": attempt + 1,
                      "group_id": manifest["group_id"]})
        return validated
    raise last_error or B22MotionError("B2.2 output could not be validated")


def adapt_b22_motion(
    output: Path,
    timeline: dict[str, Any],
    jobs: list[Any],
    context_images: dict[tuple[int, str], Path],
    *,
    provider: Any | None = None,
    model: str | None = None,
) -> dict[str, str]:
    """Adapt the *effective post-B3* prompts before issuing any LTX requests.

    Whole-video text context is supplied to every bounded group, including later
    beats. Video start images and nearby stills provide the visual evidence.
    No paid LTX manifest is created until every group has passed validation.
    """
    shots = _shots(jobs, timeline)
    eligible = [shot for shot in shots if shot.eligible]
    if not eligible:
        return {}
    instructions = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_2_professional.md").read_text(encoding="utf-8")
    )
    model = model or settings.openai_model
    provider = provider or OpenAIProvider(api_key=settings.openai_api_key)

    async def run() -> dict[str, str]:
        result: dict[str, str] = {}
        # Bounded multimodal waves, with full future text and adjacent image
        # context. A model never writes one prompt without knowing later events.
        count = (len(eligible) + _MAX_ELIGIBLE_PER_GROUP - 1) // _MAX_ELIGIBLE_PER_GROUP
        overall_started = time.monotonic()
        summaries: list[dict[str, Any]] = []
        state_path = _safe_cache(output / "video" / "b22" / "state", "progress")
        for index, first in enumerate(range(0, len(eligible), _MAX_ELIGIBLE_PER_GROUP), 1):
            group = eligible[first:first + _MAX_ELIGIBLE_PER_GROUP]
            started = time.monotonic()
            event: dict[str, Any] = {}
            print(f"  B2.2 group {index}/{count} START shots={len(group)}",
                  flush=True)
            try:
                adapted = await _call_group(
                    output=output, shots=shots, selected=group,
                    timeline=timeline, context_images=context_images,
                    model=model, provider=provider, instructions=instructions,
                    on_event=event.update,
                )
            except Exception as error:
                elapsed = round(time.monotonic() - started, 3)
                print(f"  B2.2 group {index}/{count} FAILED "
                      f"after {elapsed:.1f}s: {type(error).__name__}", flush=True)
                _save_diagnostic_json(state_path, {
                    "schema_version": 1, "status": "failed",
                    "groups_completed": index - 1, "groups_total": count,
                    "group_elapsed_seconds": elapsed,
                    "total_elapsed_seconds": round(time.monotonic() - overall_started, 3),
                })
                raise
            result.update(adapted)
            elapsed = round(time.monotonic() - started, 3)
            group_record = {
                "group_index": index, "adapted": event["adapted"],
                "cache_hit": event["cache_hit"], "attempts": event["attempts"],
                "elapsed_seconds": elapsed,
            }
            summaries.append(group_record)
            print(
                f"  B2.2 group {index}/{count} DONE shots={event['adapted']} "
                f"elapsed={elapsed:.1f}s cache={'hit' if event['cache_hit'] else 'miss'} "
                f"attempts={event['attempts']}",
                flush=True,
            )
            _save_diagnostic_json(state_path, {
                "schema_version": 1, "status": "running" if index < count else "completed",
                "groups_completed": index, "groups_total": count,
                "adapted_shots": len(result), "groups": summaries,
                "total_elapsed_seconds": round(time.monotonic() - overall_started, 3),
            })
        return result

    return asyncio.run(run())
