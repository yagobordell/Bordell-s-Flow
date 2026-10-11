"""One logical Professional Agents SDK director, cut-first and source-safe.

B1.1 groups -> global B1.2 semantic cuts -> global audiovisual plan ->
whole-video editorial review -> targeted corrections -> deterministic delivery.
No per-group model calls, fixed rhythm quotas, or repeated SDK chat-history
injection. Preserve each accepted output in legacy B-stage folders and
versioned phase folders, even if a subsequent phase fails.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import Field

from ai_video_factory.agents.professional.beats import (
    WholeScriptCuts,
    combine_cut_corrections,
    validate_and_materialize_cuts,
)
from ai_video_factory.agents.professional.prompt_loader import load_phase_instructions
from ai_video_factory.agents.professional.tools import ProfessionalPlanWorkspace
from ai_video_factory.agents.professional.visuals import (
    VisualAlignmentRepair,
    VisualCorrection,
    WholeVideoVisuals,
    apply_correction,
    assemble_plan,
    merge_alignment_repair,
    mismatched_visual_blocks,
)
from ai_video_factory.bots.contracts import (
    B11Output,
    B12Output,
    BlockMeta,
    NarrativeCore,
    StrictModel,
)


class GroupProposal(StrictModel):
    """Success-only B1.1 group boundaries."""

    narrative_core: NarrativeCore
    blocks: list[BlockMeta] = Field(min_length=1)


class EditorialFinding(StrictModel):
    beat_id: str | None
    problem: str
    correction: str


class CreativeReview(StrictModel):
    """Editorial diagnosis only: Python owns all structural validation."""

    whole_video_assessment: str
    issues: list[EditorialFinding]


def _save(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"Unsafe Professional checkpoint: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    if temp.is_symlink():
        raise ValueError(f"Unsafe Professional temporary checkpoint: {temp}")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _phase_file(root: Path, stage: str, name: str = "output.json") -> Path:
    return root / "phases" / stage / name


def _visual_input(
    blocks: list[dict[str, Any]], core: dict[str, Any],
    avatar: bytes, ebook: bytes | None,
) -> list[dict[str, Any]]:
    """One exact source copy: concatenated beat text, plus actual reference images."""
    content: list[dict[str, Any]] = [{
        "type": "input_text",
        "text": (
            "COMPLETE IMMUTABLE ORIGINAL SCRIPT AS VERBATIM ORDERED BEAT TEXTS. "
            "These texts concatenate to the authoritative complete script. "
            "Create a visual for EACH beat ID; do not retype narration.\n"
            + json.dumps({
                "narrative_core": core,
                "source_beats": blocks,
                "ebook_cover_available": ebook is not None,
            }, ensure_ascii=False)
        ),
    }, {
        "type": "input_text",
        "text": (
            "Authentic talking-presenter identity reference. "
            "Do not recreate the original narrator's room for generated B-roll."
        ),
    }, {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(avatar).decode("ascii"),
    }]
    if ebook is None:
        content.insert(0, {
            "type": "input_text",
            "text": (
                "CAPABILITY GATE: NO AUTHENTIC EBOOK COVER PROVIDED. "
                "All CTA metadata fields MUST be null. "
                "Do not invent a book cover, ebook CTA or protected ebook asset; "
                "preserve the script's actual narration."
            ),
        })
    else:
        content.extend([{
            "type": "input_text",
            "text": "Authentic ebook cover, valid only for actual spoken ebook promotion.",
        }, {
            "type": "input_image",
            "image_url": "data:image/png;base64,"
            + base64.b64encode(ebook).decode("ascii"),
        }])
    return [{"role": "user", "content": content}]


def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
    rhythm = report.get("rhythm_map") or {}
    return {
        "status": report.get("status"),
        "summary": report.get("summary"),
        "issues": report.get("issues", []),
        "rhythm_map": {
            "basis": rhythm.get("basis"),
            "between_beat_changes": rhythm.get("between_beat_changes"),
            "within_beat_changes": rhythm.get("within_beat_changes"),
            "beats": rhythm.get("beats", []),
        },
    }


def _b12_checkpoints(output: Path, blocks: list[dict[str, Any]],
                     core: dict[str, Any]) -> None:
    """Mirror old B1.2 output.json per group, plus its merged payload."""
    for group in blocks:
        result = B12Output.model_validate({
            "pipeline_stage": "B1.2", "block_id": group["block_id"],
            "beats": group["beats"], "error": None,
        })
        _save(output / "B1.2" / f"block_{group['block_id']}" / "output.json",
              result.model_dump(mode="json"))
    _save(output / "B1.2" / "merged_output.json", {
        "schema_version": "b-pipeline-merged-v1",
        "pipeline_stage": "B1.2",
        "narrative_core": core, "blocks": blocks,
    })


async def run_professional_director(
    script: str, *, output: Path, avatar: bytes, ebook: bytes | None,
    model: str, memory_root: Path | None = None, max_corrections: int = 3,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Full-script Professional pipeline, gate all downstream work on Python."""
    try:
        from agents import Agent, ModelBehaviorError, RunHooks, Runner, set_default_openai_key
    except ImportError as exc:
        raise RuntimeError(
            "Missing Agents SDK. Run with the pinned `uv --with openai-agents` overlay."
        ) from exc
    if api_key:
        set_default_openai_key(api_key)
    if not script.strip() or not avatar:
        raise ValueError("Whole nonempty script and actual avatar PNG are required.")
    if output.is_symlink() or output.is_junction():
        raise ValueError("Unsafe Professional output path.")
    root = output / "agent_openai"
    if root.is_symlink():
        raise ValueError("Unsafe Professional agent checkpoint path.")
    root.mkdir(parents=True, exist_ok=True)
    if ebook is None:
        print(
            "  [OpenAI preflight] No hay portada real del ebook. "
            "La función especial CTA de ebook está desactivada; "
            "si el guion promociona un ebook, configura su portada auténtica.",
            flush=True,
        )

    source_sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
    workspace = ProfessionalPlanWorkspace(script=script, ebook_available=ebook is not None)
    director = Agent(name="Bordell Professional Director", model=model)
    usage: list[dict[str, Any]] = []

    class Meter(RunHooks):
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
            info = response.usage
            cache = getattr(getattr(info, "input_tokens_details", None),
                            "cached_tokens", 0) or 0
            self.calls.append({
                "input_tokens": int(getattr(info, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(info, "output_tokens", 0) or 0),
                "total_tokens": int(getattr(info, "total_tokens", 0) or 0),
                "cached_input_tokens": int(cache),
                "response_id": getattr(response, "response_id", None),
            })
            _save(root / "usage.json", {
                "phases": usage, "in_progress_calls": self.calls,
            })

    async def invoke(
        stage: str, data: Any, schema: Any, *,
        corrections: int = 2,
    ) -> Any:
        # Exactly one SDK model turn/request per attempt. No Sessions: they
        # duplicated complete earlier scripts, images, tool reports and plans.
        # The SAME logical director receives source truth per global phase.
        actor = director.clone(
            instructions=load_phase_instructions(
                stage, memory_root=memory_root,
            ),
            output_type=schema,
            tools=[],
        )
        for attempt in range(corrections + 1):
            print(f"\n  [OpenAI] Fase {stage}, intento {attempt + 1}/{corrections + 1}",
                  flush=True)
            start = perf_counter()
            meter = Meter()
            request = copy.deepcopy(data)
            if attempt:
                repair = (
                    "Your previous response did not parse as strict JSON. "
                    "Return one valid complete schema-conforming object. "
                    "Do not change the task's original source or rules.\n"
                )
                if isinstance(request, str):
                    request = repair + request
                else:
                    request[0]["content"].insert(
                        0, {"type": "input_text", "text": repair},
                    )
            try:
                result = await Runner.run(
                    actor, request, max_turns=1, hooks=meter,
                )
            except ModelBehaviorError:
                entry = {
                    "phase": stage, "status": "invalid_json",
                    "attempt": attempt + 1,
                    "elapsed_seconds": round(perf_counter() - start, 3),
                    "calls": meter.calls,
                    "input_tokens": sum(x["input_tokens"] for x in meter.calls),
                    "output_tokens": sum(x["output_tokens"] for x in meter.calls),
                    "total_tokens": sum(x["total_tokens"] for x in meter.calls),
                    "requests": len(meter.calls),
                }
                usage.append(entry)
                _save(root / "usage.json", {"phases": usage})
                _save(_phase_file(root, stage, f"failed_json_{attempt + 1}.json"), entry)
                if attempt == corrections:
                    raise RuntimeError(
                        f"{stage}: respuesta JSON inválida tras "
                        f"{corrections + 1} intentos. Ver agent_openai/usage.json."
                    ) from None
                continue
            except Exception as exc:
                entry = {
                    "phase": stage, "status": "failed",
                    "attempt": attempt + 1, "error": type(exc).__name__,
                    "calls": meter.calls,
                    "input_tokens": sum(x["input_tokens"] for x in meter.calls),
                    "output_tokens": sum(x["output_tokens"] for x in meter.calls),
                    "total_tokens": sum(x["total_tokens"] for x in meter.calls),
                }
                usage.append(entry)
                _save(root / "usage.json", {"phases": usage})
                raise RuntimeError(
                    f"{stage} falló ({type(exc).__name__}); datos en "
                    "agent_openai/usage.json. Nada enviado a generación."
                ) from None
            metrics = {
                "phase": stage, "status": "passed", "attempt": attempt + 1,
                "seconds": round(perf_counter() - start, 3),
                "requests": result.context_wrapper.usage.requests,
                "input_tokens": result.context_wrapper.usage.input_tokens,
                "output_tokens": result.context_wrapper.usage.output_tokens,
                "total_tokens": result.context_wrapper.usage.total_tokens,
                "cached_input_tokens": (
                    getattr(getattr(result.context_wrapper.usage,
                                    "input_tokens_details", None),
                            "cached_tokens", 0) or 0
                ),
            }
            usage.append(metrics)
            _save(root / "usage.json", {"phases": usage})
            print(
                f"  [OpenAI {stage}] entrada={metrics['input_tokens']} "
                f"salida={metrics['output_tokens']} "
                f"total={metrics['total_tokens']} "
                f"caché={metrics['cached_input_tokens']} "
                f"solicitudes={metrics['requests']}",
                flush=True,
            )
            return result.final_output
        raise RuntimeError("Unreachable structured-output retry loop")

    # 01: original B1.1 grouping; Python owns exact materialized source.
    group_validation: dict[str, Any] = {}
    for index in range(max_corrections + 1):
        answer = await invoke(
            "01_groups",
            "GUION ORIGINAL COMPLETO, INMUTABLE:\n" + script
            + ("\n\nErrores específicos de anclajes previos:\n"
               + json.dumps(group_validation.get("issues", []), ensure_ascii=False)
               if index else ""),
            GroupProposal,
        )
        b11 = B11Output(
            pipeline_stage="B1.1",
            narrative_core=answer.narrative_core,
            blocks=answer.blocks, error=None,
        )
        group_validation = workspace.submit_groups([
            g.model_dump(mode="json") for g in answer.blocks
        ])
        _save(_phase_file(root, "01_groups", f"attempt_{index + 1}.json"), {
            "output": b11.model_dump(mode="json"),
            "validation": group_validation,
        })
        if group_validation["status"] == "passed":
            break
        print("  [01_groups] Error: " + "; ".join(
            i["message"] for i in group_validation["issues"][:5]
        ), flush=True)
    if group_validation["status"] != "passed":
        raise ValueError("B1.1 no pudo delimitar el guion; ver 01_groups.")
    core = answer.narrative_core.model_dump(mode="json")
    workspace.expected_narrative_core = copy.deepcopy(core)
    materialized_groups = group_validation["groups"]
    _save(_phase_file(root, "01_groups"), {
        "output": b11.model_dump(mode="json"),
        "validation": group_validation,
    })
    _save(root / "groups.json", {
        "source_sha256": source_sha,
        "narrative_core": core, "blocks": materialized_groups,
    })
    _save(output / "B1.1" / "output.json", b11.model_dump(mode="json"))

    # 02a: ONE global B1.2 call before any visual prompting. Return only
    # short verbatim end anchors and beat types; Python reconstructs beat text.
    cuts_validation: dict[str, Any] = {}
    cuts: WholeScriptCuts | None = None
    for index in range(max_corrections + 1):
        previous = (
            "\n\nPreviously rejected beat cuts (correct the issues, keep "
            "all valid narrative boundaries):\n"
            + json.dumps({
                "issues": cuts_validation.get("issues", []),
                "previous_cuts": cuts.model_dump(mode="json") if cuts else None,
            }, ensure_ascii=False)
        ) if index else ""
        cuts = await invoke(
            "02_beats",
            "ORIGINAL SCRIPT, AS EXACT CONSECUTIVE MATERIALIZED GROUP TEXTS. "
            "Read ALL groups before choosing any cuts; the concatenation is "
            "the original script verbatim. Source anchors are per group.\n"
            + json.dumps({
                "narrative_core": core, "groups": materialized_groups,
            }, ensure_ascii=False) + previous,
            WholeScriptCuts,
        )
        cuts_validation = validate_and_materialize_cuts(materialized_groups, cuts)
        _save(_phase_file(root, "02_beats", f"attempt_{index + 1}.json"), {
            "proposed_cuts": cuts.model_dump(mode="json"),
            "validation": cuts_validation,
        })
        if cuts_validation["status"] == "passed":
            break
        print("  [02_beats] Límites rechazados: " + "; ".join(
            x["message"] for x in cuts_validation["issues"][:5]
        ), flush=True)
    if cuts_validation["status"] != "passed" or cuts is None:
        raise ValueError(
            "No se han podido validar todos los beats ANTES de generar visuales. "
            "Los fallos y límites están guardados en agent_openai/phases/02_beats."
        )

    source_beats = cuts_validation["blocks"]
    _save(_phase_file(root, "02_beats"), {
        "cuts": cuts.model_dump(mode="json"),
        "output": source_beats, "validation": cuts_validation,
    })
    _save(root / "beats.json", {
        "source_sha256": source_sha, "narrative_core": core,
        "blocks": source_beats,
    })
    _b12_checkpoints(output, source_beats, core)

    # 02b: complete-video visual planning; source beat IDs and text are
    # immutable and not repeated in the model's structured visual output.
    catalog = _visual_input(source_beats, core, avatar, ebook)
    visuals = await invoke("02_visuals", catalog, WholeVideoVisuals)
    _save(_phase_file(root, "02_visuals", "attempt_1.json"), {
        "output": visuals.model_dump(mode="json"),
        "mismatched_blocks": mismatched_visual_blocks(source_beats, visuals),
    })
    for index in range(2):
        mismatched = mismatched_visual_blocks(source_beats, visuals)
        if not mismatched:
            break
        repair_input = copy.deepcopy(catalog)
        repair_input[0]["content"].insert(0, {
            "type": "input_text",
            "text": (
                "Your prior visual response omitted, duplicated or misordered "
                "the following B1.2 groups. Return exactly these complete "
                "VisualGroup decisions, with unchanged exact beat IDs, in "
                "ONE response covering ALL affected groups. Do not redraw "
                "anything else or modify any source narration. "
                + json.dumps({
                    "affected_block_ids": mismatched,
                    "prior_visual_groups": [
                        block.model_dump(mode="json")
                        for block in visuals.blocks
                    ],
                }, ensure_ascii=False)
            ),
        })
        corrected_visuals = await invoke(
            "02_visuals", repair_input, VisualAlignmentRepair,
        )
        try:
            visuals = merge_alignment_repair(
                source_beats, visuals, corrected_visuals, mismatched,
            )
        except ValueError as exc:
            _save(_phase_file(root, "02_visuals",
                              f"repair_{index + 1}.json"), {
                "status": "blocked", "error": str(exc),
                "output": corrected_visuals.model_dump(mode="json"),
            })
            if index == 1:
                raise RuntimeError(
                    "Global visual correction still does not align with "
                    "the validated B1.2 beats. See 02_visuals/repair_2.json."
                ) from None
            continue
        _save(_phase_file(root, "02_visuals",
                          f"repair_{index + 1}.json"), {
            "status": "passed",
            "output": corrected_visuals.model_dump(mode="json"),
        })
    if mismatched_visual_blocks(source_beats, visuals):
        _save(_phase_file(root, "02_visuals"), {
            "status": "blocked",
            "output": visuals.model_dump(mode="json"),
            "mismatched_blocks": mismatched_visual_blocks(source_beats, visuals),
        })
        raise ValueError(
            "Visual alignment remains invalid; see 02_visuals/output.json."
        )
    candidate = assemble_plan(
        narrative_core=core, source_beats=source_beats, visuals=visuals,
    )
    workspace.submit_plan_revision(candidate)
    if workspace.report is not None and workspace.report["status"] != "blocked":
        workspace.protected_cta_beats = {
            beat["beat_id"]: copy.deepcopy(beat)
            for group in candidate["blocks"]
            for beat in group["beats"]
            if beat.get("cta_group_id") is not None
        }
    _save(_phase_file(root, "02_visuals"), {
        "output": candidate, "validation": workspace.report,
    })
    _save(root / "plan_initial.json", {
        "plan": candidate, "validation": workspace.report,
    })
    _save(output / "B2" / "candidate_output.json", {
        "output": candidate, "validation": workspace.report,
    })

    # 03: ONE global creative review, one existing Python deterministic report.
    # No tools, no SDK session history, no repeated enormous tool outputs.
    review = await invoke(
        "03_evaluation",
        "Review the COMPLETE original script in the validated beat texts "
        "below and the accompanying visual choices. Evaluate real YouTube "
        "narrative rhythm, momentum, specificity and physical feasibility. "
        "Python deterministic failures are authoritative; do NOT invent "
        "new rhythm quotas. Return concrete, actionable editorial findings.\n"
        + json.dumps({
            "plan": workspace.candidate,
            "python_validation": _compact_report(workspace.validate_plan()),
        }, ensure_ascii=False),
        CreativeReview,
    )
    _save(_phase_file(root, "03_evaluation"), {
        "output": review.model_dump(mode="json"),
        "validation": _compact_report(workspace.validate_plan()),
    })
    _save(root / "review.json", {
        "assessment": review.model_dump(mode="json"),
        "validation": _compact_report(workspace.validate_plan()),
    })
    _save(output / "B2.1" / "review_output.json", {
        "schema_version": "professional-editorial-review-v1",
        "review": review.model_dump(mode="json"),
    })

    # 04: visual PATCHES, not a full replanning of every beat. A true
    # narrative recut must explicitly replace its group's cuts and visuals.
    changed_review = review.issues
    correction_attempts: list[dict[str, Any]] = []
    correction_feedback: list[dict[str, Any]] = []
    for index in range(max_corrections):
        report = workspace.validate_plan()
        if report["status"] != "blocked" and not changed_review:
            break
        if workspace.candidate is None:
            raise RuntimeError("Lost canonical plan before correction.")
        prev_by_id = {
            b["beat_id"]: b for group in workspace.candidate["blocks"]
            for b in group["beats"]
        }
        affected = {
            beat_id for i in report.get("issues", [])
            for beat_id in i.get("beat_ids", [])
        }
        affected.update(i.beat_id for i in changed_review if i.beat_id)
        # For global failures (avatar fraction, editorial rhythm, preflight)
        # include the whole visual manifest as context, but do not ask the
        # model to re-emit the full plan.
        manifest = [{
            "beat_id": b["beat_id"], "visual_type": b["visual_type"],
            "block_id": g["block_id"], "image_count": len(b.get("image_prompts", []))
            if isinstance(b.get("image_prompts"), list) else 1,
        } for g in workspace.candidate["blocks"] for b in g["beats"]]
        relevant = (
            list(prev_by_id.values()) if not affected
            else [b for k, b in prev_by_id.items() if k in affected]
        )
        correction_attempts.append({
            "attempt": index + 1,
            "blocking_issues_before": report.get("issues", []),
            "editorial_findings": [
                f.model_dump(mode="json") for f in changed_review
            ],
        })
        response = await invoke(
            "04_correction",
            "Apply exact, limited changes to this WHOLE-video plan, from "
            "the complete narration (source_beats), deterministic issues "
            "and editorial review. NEVER fabricate beat text. "
            "Return only visual_patches for existing beats. For a genuine "
            "narrative resegmentation, return resegmented_blocks plus "
            "full replanned visual entries ONLY for the resegmented group. "
            "Unchanged beats and groups must NOT be emitted in the output.\n"
            + json.dumps({
                "source_beats": source_beats,
                "issues": report.get("issues", []),
                "editorial_findings": [
                    x.model_dump(mode="json") for x in changed_review
                ],
                "visual_manifest": manifest,
                "relevant_visual_choices": relevant,
                "ebook_cover_available": ebook is not None,
                "previous_rejected_correction": correction_feedback,
            }, ensure_ascii=False),
            VisualCorrection,
        )
        # A reported editorial problem must not be silently erased by an
        # unrelated successful Python validation. Track requested repairs.
        patched_ids = {item.beat_id for item in response.visual_patches}
        resegmented_ids = {
            beat["beat_id"]
            for group in workspace.candidate["blocks"]
            if group["block_id"] in {
                part.block_id for part in response.resegmented_blocks
            }
            for beat in group["beats"]
        }
        delivered = patched_ids | resegmented_ids
        if not delivered and changed_review:
            feedback = {
                "rule_id": "EDITORIAL_REVIEW_UNADDRESSED",
                "message": (
                    "The whole-video review found concrete problems, but "
                    "the correction made no changes. Return targeted patches "
                    "or explicit resegmentation rather than discarding review."
                ),
            }
            correction_feedback = [feedback]
            _save(_phase_file(root, "04_correction",
                              f"attempt_{index + 1}.json"), {
                "output": response.model_dump(mode="json"),
                "validation": {"status": "blocked", "issues": [feedback]},
            })
            continue
        proposed_cuts = combine_cut_corrections(cuts, response.resegmented_blocks)
        new_beats = validate_and_materialize_cuts(materialized_groups, proposed_cuts)
        if new_beats["status"] != "passed":
            _save(_phase_file(root, "04_correction",
                              f"attempt_{index + 1}.json"), {
                "output": response.model_dump(mode="json"),
                "validation": new_beats,
            })
            correction_feedback = new_beats["issues"]
            changed_review = []
            continue
        try:
            updated_plan = apply_correction(
                current_plan=workspace.candidate,
                source_beats=new_beats["blocks"],
                correction=response,
            )
        except ValueError as exc:
            _save(_phase_file(root, "04_correction",
                              f"attempt_{index + 1}.json"), {
                "output": response.model_dump(mode="json"),
                "validation": {"status": "blocked", "issues": [{
                    "rule_id": "INVALID_CORRECTION",
                    "message": str(exc),
                }]},
            })
            correction_feedback = [{
                "rule_id": "INVALID_CORRECTION",
                "message": str(exc),
            }]
            changed_review = []
            continue
        outcome = workspace.submit_plan_revision(updated_plan)
        _save(_phase_file(root, "04_correction",
                          f"attempt_{index + 1}.json"), {
            "output": response.model_dump(mode="json"),
            "plan": updated_plan, "validation": outcome,
        })
        _save(root / f"correction_{index + 1}.json", {
            "plan": updated_plan, "validation": outcome,
        })
        # Whenever a structurally valid correction changes source cuts, all
        # subsequent attempts must use those cuts, even if visual preflight
        # remains blocked. Keep the B1.2 output files synchronized as well.
        cuts = proposed_cuts
        source_beats = new_beats["blocks"]
        if response.resegmented_blocks:
            _b12_checkpoints(output, source_beats, core)
            _save(_phase_file(root, "02_beats", "final_after_correction.json"),
                  {"cuts": cuts.model_dump(mode="json"),
                   "blocks": source_beats})
        correction_feedback = outcome.get("issues", [])
        changed_review = [
            issue for issue in changed_review
            if issue.beat_id not in delivered
            and not (issue.beat_id is None and delivered)
        ]

    _save(_phase_file(root, "04_correction"), {
        "status": workspace.validate_plan()["status"],
        "correction_attempts": correction_attempts,
        "resulting_validation": _compact_report(workspace.validate_plan()),
        "skipped_model_calls_when_no_issues": not correction_attempts,
    })

    # 05: Python-only release. A green result is never asserted by the LLM.
    final_report = workspace.validate_plan()
    if final_report["status"] == "blocked":
        _save(_phase_file(root, "05_delivery"), {
            "status": "blocked", "validation": final_report,
        })
        _save(root / "blocked.json", {"validation": final_report})
        codes = sorted(set(i["rule_id"] for i in final_report["issues"]))
        raise ValueError(
            "Professional director cannot deliver (" + ", ".join(codes)
            + "). Complete report: agent_openai/phases/05_delivery/output.json"
        )

    projection = workspace.deliver()
    final_plan = workspace.candidate
    if final_plan is None:
        raise RuntimeError("No canonical Professional plan to publish.")
    if final_plan["narrative_core"] != core:
        raise ValueError("Professional B1.1 narrative_core changed.")
    _save(root / "approved_plan.json", {
        "source_sha256": source_sha,
        "groups": materialized_groups, "plan": final_plan,
        "validation": final_report, "usage": usage,
    })
    _save(root / "editor_plan.json", projection["editor_plan"])
    _save(_phase_file(root, "05_delivery"), {
        "status": "passed",
        "source_sha256": source_sha,
        "plan": final_plan, "validation": final_report,
    })
    _save(output / "B2" / "merged_output.json", projection["b2_merged"])
    for block in projection["b2_merged"]["blocks"]:
        block_payload = {
            "pipeline_stage": "B2", "narrative_core": core,
            "blocks": [block], "error": None,
        }
        _save(output / "B2" / f"block_{block['block_id']}" / "output.json",
              block_payload)
    _save(output / "visual_plan.json", projection["visual_plan"])
    print("  [OpenAI] Plan completo validado y outputs por fase guardados.",
          flush=True)
    return {
        "b11": b11, "groups": materialized_groups,
        "projection": projection, "usage": usage,
        "validation": final_report,
    }
