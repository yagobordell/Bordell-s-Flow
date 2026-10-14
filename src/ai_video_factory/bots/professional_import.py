"""Validated import of a finished, application-owned professional B2 merge.

This is a separate input contract: it never manufactures B1.1/B1.2 checkpoints
or replays a B bot. The imported plan is immutable throughout this run.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ai_video_factory.bots.contracts import (
    NarrativeCore,
    ProfessionalB2Output,
)
from ai_video_factory.bots.professional_cta import ProfessionalCTAVisualBeat
from ai_video_factory.bots.workflow import (
    _beat_suffix,
    _overlong_avatar_runs,
    _professional_grid_preflight,
)


@dataclass(frozen=True)
class ImportedProfessionalB2:
    merged_output: dict[str, Any]
    narration: str
    cta_groups: tuple[tuple[str, ...], ...]

    @property
    def core(self) -> dict[str, Any]:
        return self.merged_output["narrative_core"]

    @property
    def blocks(self) -> list[dict[str, Any]]:
        return self.merged_output["blocks"]


def load_imported_professional_b2(text: str) -> ImportedProfessionalB2 | None:
    """Detect a B2 JSON document in a TXT, then fail closed on bad contracts.

    All other input continues through the original plain-script pipeline.
    """
    try:
        obj = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        if text.lstrip("\ufeff \t\r\n").startswith("{") and (
            '"b-pipeline-merged-v1"' in text or '"pipeline_stage"' in text
        ):
            raise ValueError(f"Malformed merged B2 JSON: {exc}") from exc
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("pipeline_stage") != "B2" and obj.get("schema_version") != "b-pipeline-merged-v1":
        return None
    if obj.get("schema_version") != "b-pipeline-merged-v1" or obj.get("pipeline_stage") != "B2":
        raise ValueError("Only b-pipeline-merged-v1 B2 JSON can start the professional continuation")
    if obj.get("visual_style") != "profesional":
        raise ValueError("An imported B2 must declare visual_style=profesional")
    if set(obj) != {"schema_version", "pipeline_stage", "visual_style", "narrative_core", "blocks"}:
        raise ValueError("Imported B2 has missing or unsupported top-level fields")
    try:
        NarrativeCore.model_validate(obj["narrative_core"])
        blocks = obj["blocks"]
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("Imported B2 requires a nonempty list of blocks")
        outputs: list[ProfessionalB2Output] = []
        ordered_cta: list[tuple[int, str]] = []
        all_ids: set[str] = set()
        parts: list[str] = []
        for index, block in enumerate(blocks, start=1):
            if not isinstance(block, dict) or block.get("block_id") != index:
                raise ValueError(f"Imported B2 block IDs must be consecutive from 1 (block {index})")
            beats = block.get("beats")
            if not isinstance(beats, list) or not beats:
                raise ValueError(f"Imported B2 block {index} has no beats")
            plain_beats: list[dict[str, Any]] = []
            for number, beat in enumerate(beats, start=1):
                if not isinstance(beat, dict):
                    raise ValueError(f"Imported B2 block {index} has a malformed beat")
                bid = beat.get("beat_id")
                if bid != f"{index}{_beat_suffix(number)}":
                    raise ValueError(f"Imported B2 has an invalid beat ID: {bid!r}")
                if bid in all_ids:
                    raise ValueError(f"Imported B2 duplicates beat ID {bid}")
                all_ids.add(bid)
                # Original narration is reconstructed exclusively from frozen B2 beats.
                spoken = beat.get("text")
                if not isinstance(spoken, str) or not spoken.strip():
                    raise ValueError(f"Imported B2 beat {bid} has empty narration")
                parts.append(spoken)
                plain = dict(beat)
                has_group = "cta_group_id" in plain
                has_sources = "cta_reference_sources" in plain
                if has_group != has_sources:
                    raise ValueError(f"Imported B2 beat {bid} has incomplete CTA metadata")
                if has_group:
                    group_id = plain.pop("cta_group_id")
                    sources = plain.pop("cta_reference_sources")
                    if type(group_id) is not int or group_id < 1:
                        raise ValueError(f"Imported B2 beat {bid} has an invalid CTA group")
                    ProfessionalCTAVisualBeat.model_validate({
                        key: plain[key] for key in (
                            "beat_id", "visual_type", "prompt", "video_prompt",
                            "reference", "audio",
                        )
                    } | {"reference_sources": sources})
                    ordered_cta.append((group_id, bid))
                plain_beats.append(plain)
            parsed = ProfessionalB2Output.model_validate({
                "pipeline_stage": "B2",
                "narrative_core": obj["narrative_core"],
                "blocks": [{**block, "beats": plain_beats}],
                "error": None,
            })
            outputs.append(parsed)
        if not "".join(parts).strip():
            raise ValueError("Imported B2 narration is empty")
        groups: list[list[str]] = []
        completed_ids: set[int] = set()
        for group_id, bid in ordered_cta:
            if group_id != len(groups):
                if group_id != len(groups) + 1 or group_id in completed_ids:
                    raise ValueError("Imported CTA groups must be sequential and non-overlapping")
                if groups:
                    completed_ids.add(len(groups))
                groups.append([])
            groups[-1].append(bid)
        # CTA group beats must be adjacent in the *whole* film, not merely
        # adjacent among CTA-labeled beats.
        flattened = [beat["beat_id"] for block in blocks for beat in block["beats"]]
        positions = {beat_id: idx for idx, beat_id in enumerate(flattened)}
        for group in groups:
            location = [positions[bid] for bid in group]
            if location != list(range(location[0], location[0] + len(location))):
                raise ValueError("Imported CTA group spans non-CTA narration")
        overflow = _overlong_avatar_runs(outputs)
        if overflow:
            raise ValueError("Imported B2 has overlong consecutive presenter audio runs")
        _professional_grid_preflight({"visual_style": "profesional", "blocks": blocks})
        return ImportedProfessionalB2(
            merged_output=copy.deepcopy(obj),
            narration="".join(parts),
            cta_groups=tuple(tuple(group) for group in groups),
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValueError(f"Invalid professional merged B2 input: {exc}") from exc
