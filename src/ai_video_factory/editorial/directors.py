"""Metered, content-addressed E1 and E2 structured model calls."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from ai_video_factory.config import settings
from ai_video_factory.editorial.contracts import E1Output, E2Output
from ai_video_factory.providers.openai import OpenAIProvider


logger = logging.getLogger(__name__)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write(path: Path, value: object) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Unsafe linked editorial checkpoint")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid4().hex + ".part")
    try:
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _instructions(filename: str) -> str:
    return files("ai_video_factory.bots.prompts").joinpath(filename).read_text(encoding="utf-8")


def _digest(model: str, instructions: str, payload: object) -> str:
    data = {"model": model, "instructions": instructions, "payload": payload}
    return hashlib.sha256(_canonical(data)).hexdigest()


def _usage(response: Any) -> dict[str, Any]:
    data = getattr(response, "usage", None)
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    return {
        "response_id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "service_tier": getattr(response, "service_tier", None),
        "usage": data,
    }


def e1_payload(plan: dict[str, Any], timeline: dict[str, Any]) -> dict[str, Any]:
    keyed = {beat["beat_id"]: beat for beat in timeline["beats"]}
    return {
        "schema_version": "editorial-e1-input-v1",
        "narrative_core": plan["narrative_core"],
        "blocks": [
            {
                "block_id": block["block_id"],
                "type": block.get("type"),
                "beats": [
                    {
                        "beat_id": beat["beat_id"],
                        "text": beat["text"],
                        "beat_type": beat.get("beat_type"),
                        "visual_type": beat["visual_type"],
                        "prompt": beat.get("prompt"),
                        "video_prompt": beat.get("video_prompt"),
                        "second_prompt": beat.get("second_prompt"),
                        "visual_subshots": beat.get("visual_subshots"),
                        "grid_id": beat.get("grid_id"),
                        "cta_group_id": beat.get("cta_group_id"),
                        "start_frame": keyed[beat["beat_id"]]["start_frame"],
                        "end_frame": keyed[beat["beat_id"]]["end_frame"],
                    }
                    for beat in block["beats"]
                ],
            }
            for block in plan["blocks"]
        ],
        "available_operations": [
            "progressive_text", "number_emphasis", "accumulating_list",
            "text_card", "comparison_cards", "process_flow",
        ],
        "protected_cta_groups": plan.get("ebook_cta_groups", []),
    }


async def run_e1(
    output: Path, plan: dict[str, Any], timeline: dict[str, Any], *,
    provider: Any | None = None, model: str | None = None,
) -> tuple[E1Output, str]:
    model = model or settings.openai_model
    instructions = _instructions("e1_professional.md")
    payload = e1_payload(plan, timeline)
    key = _digest(model, instructions, payload)
    cache = output / "editorial" / "e1" / (key + ".json")
    if cache.is_symlink():
        raise ValueError("Unsafe E1 cache")
    if cache.is_file():
        return E1Output.model_validate_json(cache.read_text(encoding="utf-8")), key
    provider = provider or OpenAIProvider(api_key=settings.openai_api_key)
    submitted = json.dumps(payload, ensure_ascii=False)
    try:
        answer, raw = await provider.generate_structured_with_response(
            model=model, instructions=instructions,
            input_text=submitted, output_type=E1Output,
        )
    except ValidationError as exc:
        # Responses.parse validates the entire Pydantic model before returning.
        # One bounded repair call prevents a single malformed effect from
        # terminating a completed video; infrastructure errors are not retried.
        logger.warning(
            "E1 output failed structured validation; attempting one repair: %s",
            str(exc)[:400],
        )
        repair = (
            "\\n\\nStructured-output repair: Return a fully valid result under "
            "the supplied schema. Preserve the video narrative, spoken anchors "
            "and editing purpose. Adapt the selection and grouping of elements "
            "to satisfy the exact schema constraints."
        )
        answer, raw = await provider.generate_structured_with_response(
            model=model, instructions=instructions + repair,
            input_text=submitted, output_type=E1Output,
        )
    if answer.schema_version != "e1-v1":
        raise ValueError("E1 version mismatch")
    _write(cache.with_suffix(".input.json"), payload)
    _write(cache, answer.model_dump(mode="json"))
    _write(cache.with_suffix(".usage.json"), _usage(raw))
    return answer, key


async def run_e2(
    output: Path, intervention: dict[str, Any], frames: list[tuple[str, Path]],
    protected: list[dict[str, Any]], *, provider: Any | None = None,
    model: str | None = None,
) -> tuple[E2Output, str]:
    model = model or settings.openai_model
    instructions = _instructions("e2_professional.md")
    manifest = {
        "intervention": intervention,
        "images": [
            {"image_id": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in frames
        ],
        "protected_regions": protected,
        "frame_size": {"width": 1920, "height": 1080},
    }
    key = _digest(model, instructions, manifest)
    cache = output / "editorial" / "e2" / (key + ".json")
    if cache.is_symlink():
        raise ValueError("Unsafe E2 cache")
    if cache.is_file():
        return E2Output.model_validate_json(cache.read_text(encoding="utf-8")), key
    urls = [
        "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        for _name, path in frames
    ]
    provider = provider or OpenAIProvider(api_key=settings.openai_api_key)
    answer, raw = await provider.generate_structured_with_response(
        model=model, instructions=instructions, input_text=json.dumps(manifest),
        input_image_urls=urls, output_type=E2Output,
    )
    if answer.schema_version != "e2-v1" or [
        decision.id for decision in answer.decisions
    ] != [intervention["id"]]:
        raise ValueError("E2 response changes intervention IDs")
    _write(cache.with_suffix(".input.json"), manifest)
    _write(cache, answer.model_dump(mode="json"))
    _write(cache.with_suffix(".usage.json"), _usage(raw))
    return answer, key
