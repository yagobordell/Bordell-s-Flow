"""Deterministic adapter between one frozen Agent B plan and existing consumers.

These models are internal compatibility views. No B1.2/B2/B2.1 creative bot
calls or public fake-stage outputs are made.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ai_video_factory.bots.contracts import (
    B11Output,
    B12Output,
    ProfessionalB2Block,
    ProfessionalB2Output,
    ProfessionalVisualBeat,
)
from ai_video_factory.bots.professional_cta import (
    ProfessionalCTADetection,
    ProfessionalCTAVisualBeat,
)
from ai_video_factory.bots.professional_cta_workflow import validate_cta_detection

_VISUAL_FIELDS = (
    "beat_id", "text", "beat_type", "visual_type",
    "prompt", "video_prompt", "reference", "audio",
)


class AgentBIntegrationError(ValueError):
    """Rejected bridge: do not advance the video pipeline to paid work."""


def legacy_consumer_views(
    plan: dict[str, Any], b11: B11Output,
) -> tuple[tuple[B12Output, ...], tuple[ProfessionalB2Output, ...]]:
    """Present typed beat/audio/voice views without fabricating bot invocations."""
    if b11.blocks is None or b11.narrative_core is None:
        raise AgentBIntegrationError("A validated B1.1 source is required.")
    if plan.get("narrative_core") != b11.narrative_core.model_dump(mode="json"):
        raise AgentBIntegrationError("Agent B changed B1.1 narrative metadata.")
    if len(plan["blocks"]) != len(b11.blocks):
        raise AgentBIntegrationError("Agent B block count differs from B1.1.")
    b12 = []
    b2 = []
    for source, block in zip(b11.blocks, plan["blocks"], strict=True):
        if (
            block["block_id"] != source.block_id
            or block["type"] != source.type
            or block["emotional_entry"] != source.emotional_entry
            or block["emotional_exit"] != source.emotional_exit
        ):
            raise AgentBIntegrationError("Agent B changed B1.1 block identity.")
        values = [{key: beat[key] for key in _VISUAL_FIELDS} for beat in block["beats"]]
        typed_visuals = [ProfessionalVisualBeat.model_validate(beat) for beat in values]
        b12.append(B12Output.model_validate({
            "pipeline_stage": "B1.2",
            "block_id": source.block_id,
            "beats": [
                {"beat_id": beat.beat_id, "text": beat.text, "beat_type": beat.beat_type}
                for beat in typed_visuals
            ],
            "error": None,
        }))
        b2.append(ProfessionalB2Output.model_validate({
            "pipeline_stage": "B2",
            "narrative_core": b11.narrative_core.model_dump(mode="json"),
            "blocks": [ProfessionalB2Block(
                block_id=source.block_id, type=source.type,
                emotional_entry=source.emotional_entry,
                emotional_exit=source.emotional_exit,
                span=source.span, beats=typed_visuals,
            ).model_dump(mode="json")],
            "error": None,
        }))
    return tuple(b12), tuple(b2)


def declared_cta_groups(plan: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    """CTA metadata is source-owned and must be complete before downstream use."""
    groups: dict[int, list[str]] = {}
    previous: int | None = None
    completed: set[int] = set()
    for block in plan["blocks"]:
        for beat in block["beats"]:
            group = beat.get("cta_group_id")
            if group is None:
                previous = None
                continue
            if type(group) is not int or group < 1:
                raise AgentBIntegrationError("Invalid CTA group number.")
            if group != previous:
                if group in completed:
                    raise AgentBIntegrationError("CTA group is not contiguous.")
                completed.add(group)
            previous = group
            sources = beat.get("cta_reference_sources")
            if not isinstance(sources, list):
                raise AgentBIntegrationError("CTA beat requires explicit reference_sources.")
            ProfessionalCTAVisualBeat.model_validate({
                "beat_id": beat["beat_id"],
                "visual_type": beat["visual_type"], "prompt": beat["prompt"],
                "video_prompt": beat["video_prompt"], "reference": beat["reference"],
                "audio": beat["audio"], "reference_sources": sources,
            })
            groups.setdefault(group, []).append(beat["beat_id"])
    if sorted(groups) != list(range(1, len(groups) + 1)):
        raise AgentBIntegrationError("CTA group IDs must be consecutive starting at 1.")
    return tuple(tuple(groups[k]) for k in sorted(groups))


def assert_cta_detector_match(
    plan: dict[str, Any], expected: Sequence[Sequence[str]],
) -> tuple[tuple[str, ...], ...]:
    """The auxiliary detector checks ownership, not creative visual direction."""
    groups = declared_cta_groups(plan)
    detection = ProfessionalCTADetection(cta_groups=[list(g) for g in expected])
    merged = {
        "blocks": [{"beats": b["beats"]} for b in plan["blocks"]],
    }
    detected = validate_cta_detection(detection, merged)
    if detected != groups:
        raise AgentBIntegrationError(
            "CTA detector and frozen Agent B plan disagree about protected beats. "
            "Do not generate paid media. Re-plan in a fresh project after inspecting "
            "the CTA ownership feedback."
        )
    return groups
