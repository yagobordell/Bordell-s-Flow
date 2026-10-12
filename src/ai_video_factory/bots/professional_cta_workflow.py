"""Isolated professional ebook-CTA detection, visual direction and plan merge.

B2 runs normally. The detector reads merged B1.2; only complete existing
beat IDs are routed. Visual overrides are applied AFTER B2 and BEFORE B2.1;
the original B2 per-block artifacts remain available for auditing.
"""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Callable
from importlib.resources import files
from typing import Any

from ai_video_factory.bots.contracts import ProfessionalVisualBeat
from ai_video_factory.bots.professional_cta import (
    ProfessionalCTADetection,
    ProfessionalCTAVisualBeat,
    ProfessionalCTAVisuals,
)
from ai_video_factory.bots.validation_retries import MAX_VALIDATION_RETRIES
from ai_video_factory.providers.grid_numbering import (
    GridMarker,
    parse_grid_marker,
    reflow_lettered_groups_at_cta_boundaries,
)


def prompt_instructions(name: str) -> str:
    return files("ai_video_factory.bots.prompts").joinpath(name).read_text(encoding="utf-8")


def _legacy_grid_marker(prompt: object) -> GridMarker | None:
    marker = parse_grid_marker(prompt)
    return marker if marker is not None and marker.style == "legacy" else None


def validate_cta_detection(
    detection: ProfessionalCTADetection, merged_b12: dict[str, Any],
) -> tuple[tuple[str, ...], ...]:
    ordered = [
        beat["beat_id"] for block in merged_b12["blocks"] for beat in block["beats"]
    ]
    if len(ordered) != len(set(ordered)):
        raise ValueError("B1.2 contains duplicate beat IDs")
    positions = {beat_id: index for index, beat_id in enumerate(ordered)}
    groups: list[tuple[str, ...]] = []
    seen: set[str] = set()
    previous_end = -1
    for values in detection.cta_groups:
        if not values:
            raise ValueError("CTA Detector returned an empty group")
        if any(beat_id not in positions for beat_id in values):
            raise ValueError("CTA Detector returned a nonexistent beat ID")
        indexes = [positions[beat_id] for beat_id in values]
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
            raise ValueError("CTA Detector group is not in consecutive beat order")
        if indexes[0] <= previous_end or seen.intersection(values):
            raise ValueError("CTA Detector groups overlap or are out of order")
        seen.update(values)
        previous_end = indexes[-1]
        groups.append(tuple(values))
    return tuple(groups)


def validate_cta_visuals(
    output: ProfessionalCTAVisuals, group: tuple[str, ...],
    merged_b12: dict[str, Any],
) -> dict[str, ProfessionalCTAVisualBeat]:
    expected = {
        beat["beat_id"]: beat
        for block in merged_b12["blocks"] for beat in block["beats"]
    }
    if [beat.beat_id for beat in output.beats] != list(group):
        raise ValueError("B2 CTA changed beat IDs, order or count")
    for visual in output.beats:
        source = expected[visual.beat_id]
        ProfessionalVisualBeat.model_validate({
            "beat_id": visual.beat_id, "text": source["text"],
            "beat_type": source["beat_type"],
            "visual_type": visual.visual_type, "prompt": visual.prompt,
            "video_prompt": visual.video_prompt, "reference": visual.reference,
            "audio": visual.audio,
        })
    return {beat.beat_id: beat for beat in output.beats}


def _reflow_numbered_cta_edges(block: dict[str, Any]) -> None:
    # A B2 grid spanning a replaced beat must be cut at the CTA boundary.
    # Independent isolated numbered panels become normal, unnumbered images.
    pending: list[dict[str, Any]] = []
    previous_group: int | None = None

    def finish() -> None:
        nonlocal pending
        if pending:
            for index, beat in enumerate(pending, 1):
                marker = _legacy_grid_marker(beat["prompt"])
                if marker is None:
                    raise ValueError("Invalid numbered CTA grid")
                beat["prompt"] = (
                    f"{index}. {marker.body.strip()}" if len(pending) > 1
                    else marker.body.strip()
                )
            pending = []

    for beat in block["beats"]:
        prompt = beat.get("prompt")
        marker = _legacy_grid_marker(prompt)
        group = beat.get("cta_group_id")
        if (
            marker is None
            or (pending and group != previous_group)
        ):
            finish()
        if marker is not None:
            pending.append(beat)
        previous_group = group
    finish()
    reflow_lettered_groups_at_cta_boundaries(block)


def apply_cta_visuals(
    blocks: list[dict[str, Any]],
    groups: tuple[tuple[str, ...], ...],
    overrides: dict[str, ProfessionalCTAVisualBeat],
) -> list[dict[str, Any]]:
    result = copy.deepcopy(blocks)
    expected = {beat_id for group in groups for beat_id in group}
    if set(overrides) != expected:
        raise ValueError("CTA visual output does not cover exact detected beat IDs")
    used: set[str] = set()
    membership = {
        beat_id: index for index, group in enumerate(groups, start=1)
        for beat_id in group
    }
    for block in result:
        for index, beat in enumerate(block["beats"]):
            beat_id = beat["beat_id"]
            if beat_id not in overrides:
                continue
            visual = overrides[beat_id]
            if block["type"] == "close" and index == len(block["beats"]) - 1:
                if not (
                    visual.visual_type == "avatar"
                    and visual.prompt is None
                    and not visual.reference and visual.audio
                ):
                    raise ValueError("CTA director must preserve final close avatar anchor")
            beat.update(visual.model_dump(exclude={"beat_id", "reference_sources"}))
            beat["cta_group_id"] = membership[beat_id]
            beat["cta_reference_sources"] = visual.reference_sources
            used.add(beat_id)
        _reflow_numbered_cta_edges(block)
    if used != expected:
        raise ValueError("CTA override contained unknown beats")
    return result


async def detect_professional_ctas(
    merged_b12: dict[str, Any], *,
    provider: Any, model: str, semaphore: Any,
    on_input: Callable[..., None] | None = None,
    on_output: Callable[..., None] | None = None,
    on_api_response: Callable[..., None] | None = None,
    on_call_duration: Callable[..., None] | None = None,
) -> tuple[tuple[str, ...], ...]:
    from time import perf_counter

    stage = "CTA Detector"
    payload = merged_b12
    instructions = prompt_instructions("cta_detector_professional.md")
    error_detail = ""
    for attempt in range(MAX_VALIDATION_RETRIES + 1):
        if on_input is not None:
            on_input(stage, None, payload if attempt == 0 else {
                **payload, "validation_feedback": error_detail,
            })
        start = perf_counter()
        async with semaphore:
            response, raw = await provider.generate_structured_with_response(
                model=model,
                instructions=instructions + (
                    "\n\nRepair this validation failure: " + error_detail
                    if error_detail else ""
                ),
                input_text=json.dumps(payload, ensure_ascii=False),
                output_type=ProfessionalCTADetection,
            )
        if on_api_response is not None:
            on_api_response(stage, None, raw)
        if on_call_duration is not None:
            on_call_duration(stage, None, perf_counter() - start)
        try:
            groups = validate_cta_detection(response, merged_b12)
        except ValueError as exc:
            error_detail = str(exc)
            continue
        if on_output is not None:
            on_output(stage, None, response.model_dump())
        return groups
    raise ValueError("CTA Detector failed validation: " + error_detail)


async def direct_professional_cta(
    group_id: int, beat_ids: tuple[str, ...],
    merged_b12: dict[str, Any], *,
    presenter: bytes, ebook: bytes,
    provider: Any, model: str, semaphore: Any,
    on_input: Callable[..., None] | None = None,
    on_output: Callable[..., None] | None = None,
    on_api_response: Callable[..., None] | None = None,
    on_call_duration: Callable[..., None] | None = None,
    correction: str = "",
    previous_visuals: dict[str, ProfessionalCTAVisualBeat] | None = None,
) -> dict[str, ProfessionalCTAVisualBeat]:
    from time import perf_counter

    available = {
        beat["beat_id"]: {**beat, "block_id": block["block_id"],
                          "block_type": block.get("type")}
        for block in merged_b12["blocks"] for beat in block["beats"]
    }
    payload = {
        "narrative_core": merged_b12["narrative_core"],
        "beats": [available[beat_id] for beat_id in beat_ids],
        "qr_overlay": {
            "position": "top_right", "inserted_by": "final_compositor",
            "after_first_cta_seconds": 90,
        },
        "references": ["presenter", "ebook"],
    }
    if previous_visuals:
        payload["previous_visuals"] = [
            previous_visuals[beat_id].model_dump()
            for beat_id in beat_ids if beat_id in previous_visuals
        ]
    images = [
        "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        for image in (presenter, ebook)
    ]
    error_detail = correction
    stage = "B2 CTA"
    for attempt in range(MAX_VALIDATION_RETRIES + 1):
        request_payload = (
            {**payload, "validation_feedback": error_detail}
            if error_detail else payload
        )
        if on_input is not None:
            on_input(stage, group_id, request_payload)
        started = perf_counter()
        async with semaphore:
            response, raw = await provider.generate_structured_with_response(
                model=model,
                instructions=prompt_instructions("b2_cta_professional.md")
                + ("\n\nRequired correction: " + error_detail if error_detail else ""),
                input_text=json.dumps(request_payload, ensure_ascii=False),
                input_image_urls=images,
                output_type=ProfessionalCTAVisuals,
            )
        if on_api_response is not None:
            on_api_response(stage, group_id, raw)
        if on_call_duration is not None:
            on_call_duration(stage, group_id, perf_counter() - started)
        try:
            checked = validate_cta_visuals(response, beat_ids, merged_b12)
        except ValueError as exc:
            error_detail = str(exc)
            continue
        if on_output is not None:
            on_output(stage, group_id, response.model_dump())
        return checked
    raise ValueError(f"B2 CTA group {group_id} failed validation: {error_detail}")
