"""Projection of the new whole-script plan into existing Professional B2 outputs.

Haiku notes live in a separate sidecar: the legacy ProfessionalB2Output contract
has extra=forbid. No Casero payloads or generator logic are called here.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from ai_video_factory.agents.professional.validation import validate_plan

_PREFIX = re.compile(r"^\s*(?:[1-9]|1[0-8])\.\s+", re.DOTALL)


def project_professional_plan(
    script: str, groups: list[dict[str, Any]], plan: dict[str, Any],
    *,
    protected_cta_beats: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Produce production, voice, and editor projections only if fully valid.

    This is a projection, not installation into the running pipeline. Existing
    image and voice workflows must be explicitly connected in a later step.
    """
    report = validate_plan(
        script, groups, plan, protected_cta_beats=protected_cta_beats
    )
    if report["status"] == "blocked":
        raise ValueError("Cannot project invalid Professional plan: " + "; ".join(
            f"{i['rule_id']}: {i['message']}" for i in report["issues"]
            if i["severity"] == "error"
        ))

    from ai_video_factory.agents.professional.validation import validate_groups
    checked = validate_groups(script, groups)
    materialized = checked["groups"]
    projected_blocks: list[dict[str, Any]] = []
    voice_blocks: list[dict[str, Any]] = []
    editor_blocks: list[dict[str, Any]] = []
    cta_groups: dict[int, list[str]] = {}

    for metadata, block in zip(materialized, plan["blocks"], strict=True):
        meta = {key: copy.deepcopy(metadata[key]) for key in (
            "block_id", "type", "emotional_entry", "emotional_exit", "span",
        )}
        visuals: list[dict[str, Any]] = []
        voice: list[dict[str, Any]] = []
        editorial: list[dict[str, Any]] = []
        for beat in block["beats"]:
            entry = {
                key: copy.deepcopy(beat[key]) for key in (
                    "beat_id", "beat_type", "text", "visual_type",
                    "audio", "reference", "video_prompt",
                )
            }
            if beat["visual_type"] == "media_image":
                prompts = beat["image_prompts"]
                mode = beat.get("generation_mode", "independent")
                cleaned = [_PREFIX.sub("", prompt).strip() for prompt in prompts]
                if len(cleaned) == 1:
                    entry["prompt"] = prompts[0]
                else:
                    numbered = [
                        f"{index}. {prompt}" if mode == "grid" else prompt
                        for index, prompt in enumerate(cleaned, start=1)
                    ]
                    entry["prompt"] = numbered[0]
                    entry["visual_subshots"] = [
                        {
                            "asset_id": f"{beat['beat_id']}__{index}",
                            "subshot_index": index,
                            "subshot_count": len(numbered),
                            "prompt": prompt,
                            "generation": mode,
                        }
                        for index, prompt in enumerate(numbered, start=1)
                    ]
            else:
                entry["prompt"] = beat.get("prompt")
            out = copy.deepcopy(entry)
            if "cta_group_id" in beat:
                out["cta_group_id"] = beat["cta_group_id"]
                out["cta_reference_sources"] = copy.deepcopy(
                    beat["cta_reference_sources"]
                )
                cta_groups.setdefault(beat["cta_group_id"], []).append(beat["beat_id"])
            visuals.append(out)
            voice.append({
                k: v for k, v in entry.items()
                if k not in {"visual_subshots"}
            })
            editorial.append({
                "beat_id": beat["beat_id"],
                "notes": copy.deepcopy(beat.get("editor_notes", [])),
            })
        projected_blocks.append({**meta, "beats": visuals})
        voice_blocks.append({**meta, "beats": voice})
        editor_blocks.append({"block_id": meta["block_id"], "beats": editorial})

    core = copy.deepcopy(plan["narrative_core"])
    production = {
        "schema_version": "b-pipeline-v1",
        "visual_style": "profesional",
        "narrative_core": core,
        "blocks": projected_blocks,
        "ebook_cta_groups": [
            cta_groups[group_id] for group_id in sorted(cta_groups)
        ],
    }
    b2 = {
        "schema_version": "b-pipeline-merged-v1",
        "pipeline_stage": "B2",
        "visual_style": "profesional",
        "narrative_core": copy.deepcopy(core),
        "blocks": voice_blocks,
    }
    editor = {
        "schema_version": "professional-editor-notes-v1",
        "source_sha256": report["source_sha256"],
        "blocks": editor_blocks,
    }

    # Strict on-the-wire voice blocks must remain loadable with no editor fields.
    from ai_video_factory.bots.contracts import ProfessionalB2Output
    for block in voice_blocks:
        ProfessionalB2Output.model_validate({
            "pipeline_stage": "B2",
            "narrative_core": core,
            "blocks": [block],
            "error": None,
        })

    # Reuse the existing grid preflight. An invalid continuity chain must not
    # reach a paid image request even when all basic fields are valid.
    from ai_video_factory.providers.image_grids import plan_image_jobs
    from ai_video_factory.providers.openai_batch_images import described_beats
    plan_image_jobs(production, described_beats(production))
    return {
        "visual_plan": production, "b2_merged": b2, "editor_plan": editor,
    }
