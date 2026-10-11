"""One logical Professional Agents SDK director: narrative AND visual planning together.

B1.1 groups -> ONE joint whole-video beat AND visual plan ->
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

from ai_video_factory.agents.professional.compact import (
    SparseCorrection as DirectionCorrection,
    SparseDirection as WholeVideoDirection,
    apply_sparse_correction as apply_direction_correction,
    materialize_sparse_direction as materialize_direction,
)
from ai_video_factory.agents.professional.preflight import (
    requires_ebook_cover,
    validate_source_assets,
)
from ai_video_factory.agents.professional.prompt_loader import load_phase_instructions
from ai_video_factory.agents.professional.tools import ProfessionalPlanWorkspace
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
    """Exactly five canonical public phases."""
    return root / "phases" / stage / name


def _direction_input(
    groups: list[dict[str, Any]], core: dict[str, Any],
    avatar: bytes, ebook: bytes | None,
) -> list[dict[str, Any]]:
    """One complete-script beat+visual decision, with real reference assets."""
    content: list[dict[str, Any]] = [{
        "type": "input_text",
        "text": (
            "ONE COMPLETE AUDIOVISUAL PLAN: decide each narrative beat boundary "
            "AND that SAME beat's visual carrier, scene continuity, still/video "
            "prompts and source-anchored editor notes TOGETHER, in one response. "
            "Read ALL B1.1 groups first. Every group text is an exact original "
            "script slice; the full sequence is the immutable narration. "
            "Return EVERY beat's actual verbatim narration in beat.text, "
            "never a source pointer, end_anchor, occurrence, or offsets. "
            "Use the compact type-specific beat schema: avatar has no image "
            "or video prompt; media_image supplies only its image prompts and "
            "generation_mode; media_video supplies frame and motion prompts; "
            "avatar_media supplies its supporting image prompt. "
            "Put editor notes, presenter identity references, and authentic "
            "CTA ownership ONLY in each group's annotation lists, never as "
            "null/empty fields repeated on beats. "
            "Copy every source word and punctuation mark exactly, in order. "
            "Whitespace formatting may differ; Python restores the original "
            "source bytes during validation. Do not omit, paraphrase or add "
            "narration. A source slice MUST be <=100 stripped characters AND "
            "<=40 words. If a long sentence cannot fit, cut at a meaningful "
            "clause without dropping any words. Verify exact coverage and "
            "each limit before returning the whole audiovisual plan.\n"
            + json.dumps({
                "narrative_core": core, "groups": groups,
                "ebook_cover_available": ebook is not None,
            }, ensure_ascii=False)
        ),
    }, {
        "type": "input_text",
        "text": (
            "Authentic presenter identity reference. Generated B-roll must "
            "depict the actor of the narration, not default to this avatar."
        ),
    }, {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(avatar).decode("ascii"),
    }]
    if ebook is not None:
        content.extend([{
            "type": "input_text",
            "text": "Authentic protected ebook cover for the actual spoken promotion.",
        }, {
            "type": "input_image",
            "image_url": "data:image/png;base64,"
            + base64.b64encode(ebook).decode("ascii"),
        }])
    else:
        content.insert(0, {
            "type": "input_text",
            "text": (
                "No ebook cover configured; invent no ebook offer, QR or cover. "
                "Preserve original narration exactly."
            ),
        })
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
    validate_source_assets(script, ebook_available=ebook is not None)
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
            logical_phase = stage
            label = stage
            print(f"\n  [OpenAI] Fase {label}, intento {attempt + 1}/{corrections + 1}",
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
                    "phase": logical_phase, "status": "invalid_json",
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
                    _save(_phase_file(root, stage), {
                        "phase": stage, "status": "blocked",
                        "error": "invalid_json", "attempts": corrections + 1,
                    })
                    raise RuntimeError(
                        f"{stage}: respuesta JSON inválida tras "
                        f"{corrections + 1} intentos. Ver agent_openai/usage.json."
                    ) from None
                continue
            except Exception as exc:
                entry = {
                    "phase": logical_phase, "status": "failed",
                    "attempt": attempt + 1, "error": type(exc).__name__,
                    "calls": meter.calls,
                    "input_tokens": sum(x["input_tokens"] for x in meter.calls),
                    "output_tokens": sum(x["output_tokens"] for x in meter.calls),
                    "total_tokens": sum(x["total_tokens"] for x in meter.calls),
                }
                usage.append(entry)
                _save(root / "usage.json", {"phases": usage})
                _save(_phase_file(root, stage), {
                    "phase": stage, "status": "failed",
                    "error_type": type(exc).__name__, "usage": entry,
                })
                raise RuntimeError(
                    f"{stage} falló ({type(exc).__name__}); datos en "
                    "agent_openai/usage.json. Nada enviado a generación."
                ) from None
            metrics = {
                "phase": logical_phase,
                "status": "passed", "attempt": attempt + 1,
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
                f"  [OpenAI {label}] entrada={metrics['input_tokens']} "
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
    for index in range(min(max_corrections, 1) + 1):
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
        _save(_phase_file(root, "01_groups"), {
            "status": "blocked", "validation": group_validation,
        })
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

    # 02: ONE global model request returns BOTH beats and their visual plans.
    # Source text remains the original Python-materialized B1.1 groups.
    joint_direction = await invoke(
        "02_planning",
        _direction_input(materialized_groups, core, avatar, ebook),
        WholeVideoDirection,
    )
    def check_direction(direction: WholeVideoDirection) -> dict[str, Any]:
        material = materialize_direction(materialized_groups, direction, core)
        if material["plan"] is not None:
            report = workspace.submit_plan_revision(material["plan"])
            material["validation"] = report
            material["status"] = report["status"]
            material["issues"].extend(report.get("issues", []))
        else:
            material["validation"] = {
                "status": "blocked",
                "issues": material["issues"],
            }
        return material

    current = check_direction(joint_direction)
    # Protect the source-backed authentic ebook passage across the correction
    # phase, not only AFTER correction. A correct CTA cannot be silently
    # relabeled, rearranged or visually replaced during later review.
    if ebook is not None and current["plan"] is not None:
        if requires_ebook_cover(script):
            protected = {
                beat["beat_id"]: copy.deepcopy(beat)
                for group in current["plan"]["blocks"]
                for beat in group["beats"]
                if beat.get("cta_group_id") is not None
            }
            if protected and not any(
                issue["rule_id"] in {
                    "CTA_COVER_UNAVAILABLE",
                    "CTA_SPOKEN_ASSET_UNPROTECTED",
                    "CTA_SPOKEN_OFFER_MISSING",
                } for issue in current["issues"]
            ):
                workspace.protected_cta_beats = protected
    initial_report = _compact_report(current["validation"])
    _save(_phase_file(root, "02_planning"), {
        "schema_version": "professional-phase-output-v1",
        "phase": "02_planning", "status": current["status"],
        "output": joint_direction.model_dump(mode="json"),
        "boundary_validation": {
            "status": current["boundaries"]["status"],
            "issues": current["boundaries"]["issues"],
            "total_beats": current["boundaries"]["total_beats"],
        },
        "validation": initial_report,
    })
    _save(root / "plan_initial.json", {
        "direction": joint_direction.model_dump(mode="json"),
        "plan": current["plan"], "boundary_validation": current["boundaries"],
        "validation": initial_report,
    })
    if current["plan"] is not None:
        _save(output / "B2" / "candidate_output.json", {
            "output": current["plan"], "validation": current["validation"],
        })

    # 03: evaluate the single, already-made complete audiovisual proposal.
    # Even an invalid cut still has its visual decision and can be repaired.
    review = await invoke(
        "03_evaluation",
        "Evaluate this ONE complete audiovisual proposal for YouTube "
        "storytelling, real actor action, narrative cadence, continuous grids, "
        "source-faithful notes and concrete Python errors. Both cuts and "
        "visual choices were proposed TOGETHER. Return precise concerns "
        "that can be resolved by targeted corrections, never a replacement "
        "rhythm quota.\n"
        + json.dumps({
            "narrative_core": core,
            "original_groups": materialized_groups,
            "combined_audiovisual_direction": joint_direction.model_dump(mode="json"),
            "python_issues": current["issues"],
            "python_validation": _compact_report(current["validation"]),
        }, ensure_ascii=False),
        CreativeReview,
    )
    _save(_phase_file(root, "03_evaluation"), {
        "output": review.model_dump(mode="json"),
        "validation": _compact_report(current["validation"]),
    })
    _save(root / "review.json", {
        "assessment": review.model_dump(mode="json"),
        "validation": _compact_report(current["validation"]),
    })
    _save(output / "B2.1" / "review_output.json", {
        "schema_version": "professional-editorial-review-v1",
        "review": review.model_dump(mode="json"),
    })

    # 04: bounded global correction proposals. A revised narrative block
    # ALWAYS carries its visuals in that SAME model response.
    immutable_cta_ids = {
        assignment.beat_id for group in joint_direction.blocks
        for assignment in group.cta
    }
    readonly_findings = [
        item for item in review.issues if item.beat_id in immutable_cta_ids
    ]
    editable_findings = [
        item for item in review.issues if item.beat_id not in immutable_cta_ids
    ]
    correction_attempts: list[dict[str, Any]] = []
    remaining_editorial: list[dict[str, Any]] = []
    outstanding_editorial = editable_findings
    if current["status"] == "blocked" or outstanding_editorial:
        if max_corrections < 1:
            raise ValueError("Whole-video direction is blocked; correction disabled.")
        # Iterative corrections remain ONE global audiovisual phase: every
        # revision is model-directed, not an automatic Python rhythm rewrite.
        # Abort on no progress rather than spend on identical failed attempts.
        for correction_number in range(1, max_corrections + 1):
            before = current
            previous_editorial = outstanding_editorial
            correction = await invoke(
                "04_correction",
                "Return ONE coherent correction covering ALL remaining structural "
                "and editorial issues. EVERY previously failing source span MUST "
                "be split at exact, meaningful verbatim anchors so each revised "
                "beat is <=100 stripped characters AND <=40 words. Do NOT "
                "merge an overlong sentence into one beat, duplicate source "
                "narration, change the narration, or create a phantom beat. "
                "Verify EACH new anchor exists after the previous one. "
                "Use replaced_groups for narrative recuts: each entire "
                "replaced group MUST include BOTH new beat anchors AND all "
                "corresponding visual choices. Use visual_patches only for "
                "unchanged boundaries. Preserve untouched blocks and protected "
                "CTA material. Return empty lists only when no edits are needed.\n"
                + json.dumps({
                    "narrative_core": core,
                    "original_groups": materialized_groups,
                    "combined_audiovisual_direction": joint_direction.model_dump(mode="json"),
                    "blocking_python_issues": current["issues"],
                    "editorial_findings": [
                        item.model_dump(mode="json") for item in outstanding_editorial
                    ],
                    "ebook_cover_available": ebook is not None,
                }, ensure_ascii=False),
                DirectionCorrection,
            )
            invalid_correction = False
            try:
                corrected_direction = apply_direction_correction(
                    joint_direction, correction,
                )
                checked = check_direction(corrected_direction)
            except (ValueError, TypeError) as exc:
                invalid_correction = True
                checked = {
                    "status": "blocked",
                    "issues": [{
                        "rule_id": "INVALID_DIRECTION_CORRECTION",
                        "message": str(exc),
                    }],
                    "validation": {
                        "status": "blocked",
                        "issues": [{
                            "rule_id": "INVALID_DIRECTION_CORRECTION",
                            "message": str(exc),
                        }],
                    },
                    "plan": None,
                    "boundaries": current["boundaries"],
                }
                corrected_direction = joint_direction

            modified = {beat.beat_id for beat in correction.visual_patches}
            modified.update(
                beat.beat_id for group in correction.replaced_groups
                for beat in group.beats
            )
            replaced_ids = {group.block_id for group in correction.replaced_groups}
            original_beat_groups = {
                beat.beat_id: group.block_id
                for group in joint_direction.blocks for beat in group.beats
            }
            outstanding_editorial = [
                item for item in outstanding_editorial
                if item.beat_id not in modified
                and original_beat_groups.get(item.beat_id) not in replaced_ids
                and not (item.beat_id is None and (modified or replaced_ids))
            ]
            remaining_editorial = [
                {"rule_id": "EDITORIAL_ISSUE_UNADDRESSED",
                 "message": item.problem, "beat_id": item.beat_id}
                for item in outstanding_editorial
            ]
            if remaining_editorial:
                checked["status"] = "blocked"
                checked["validation"]["status"] = "blocked"
                if checked["validation"]["issues"] is not checked["issues"]:
                    checked["validation"]["issues"].extend(remaining_editorial)
                checked["issues"].extend(remaining_editorial)

            _save(_phase_file(
                root, "04_correction", f"attempt_{correction_number}.json",
            ), {
                "output": correction.model_dump(mode="json"),
                "revised_direction": corrected_direction.model_dump(mode="json"),
                "validation": _compact_report(checked["validation"]),
                "boundary_validation": checked["boundaries"],
            })
            _save(root / f"correction_{correction_number}.json", {
                "direction": corrected_direction.model_dump(mode="json"),
                "plan": checked["plan"],
                "validation": _compact_report(checked["validation"]),
            })
            correction_attempts.append({
                "attempt": correction_number, "status": checked["status"],
                "issues": checked["issues"],
            })
            previous_count = len(before["issues"]) + len(previous_editorial)
            new_count = len(checked["issues"])
            if invalid_correction:
                outstanding_editorial = previous_editorial
                if previous_editorial and before["status"] != "blocked":
                    before["status"] = "blocked"
                    before["validation"]["status"] = "blocked"
                    unresolved = [
                        {"rule_id": "EDITORIAL_ISSUE_UNADDRESSED",
                         "message": item.problem, "beat_id": item.beat_id}
                        for item in previous_editorial
                    ]
                    before["issues"].extend(unresolved)
                    before["validation"]["issues"].extend(unresolved)
                break
            if checked["status"] == "passed" or (
                checked["status"] == "passed_with_warnings"
            ):
                joint_direction = corrected_direction
                current = checked
                break
            if new_count >= previous_count:
                # Keep the better previous revision and stop spending tokens.
                outstanding_editorial = previous_editorial
                remaining_editorial = [
                    {"rule_id": "EDITORIAL_ISSUE_UNADDRESSED",
                     "message": item.problem, "beat_id": item.beat_id}
                    for item in outstanding_editorial
                ]
                if remaining_editorial and before["status"] != "blocked":
                    before["status"] = "blocked"
                    before["validation"]["status"] = "blocked"
                    before["issues"].extend(remaining_editorial)
                    before["validation"]["issues"].extend(remaining_editorial)
                break
            joint_direction = corrected_direction
            current = checked
    _save(_phase_file(root, "04_correction"), {
        "phase": "04_correction", "status": current["status"],
        "correction_attempts": correction_attempts,
        "resulting_validation": _compact_report(current["validation"]),
        "protected_cta_editorial_findings": [
            item.model_dump(mode="json") for item in readonly_findings
        ],
        "proposed_plan": joint_direction.model_dump(mode="json"),
        "skipped_model_call_when_no_issues": not correction_attempts,
    })

    # 05: Python-only strict gate. Never publish an incomplete B1.2 output.
    final_report = current["validation"]
    if current["status"] == "blocked" or current["plan"] is None:
        _save(_phase_file(root, "05_delivery"), {
            "status": "blocked", "validation": _compact_report(final_report),
            "proposed_plan": joint_direction.model_dump(mode="json"),
        })
        _save(root / "blocked.json", {
            "validation": _compact_report(final_report),
        })
        codes = sorted(set(
            issue["rule_id"] for issue in current["issues"]
        ))
        raise ValueError(
            "Professional director cannot deliver (" + ", ".join(codes)
            + "). Complete report: agent_openai/phases/05_delivery/output.json"
        )

    # Both beats and visuals were planned TOGETHER, and both are valid.
    # Only NOW save the legacy B1.2 bridge, never as a false standalone plan.
    source_beats = current["boundaries"]["blocks"]
    workspace.protected_cta_beats = {
        beat["beat_id"]: copy.deepcopy(beat)
        for group in current["plan"]["blocks"]
        for beat in group["beats"] if beat.get("cta_group_id") is not None
    }
    final_report = workspace.validate_plan()
    if final_report["status"] == "blocked":
        _save(_phase_file(root, "05_delivery"), {
            "status": "blocked", "validation": _compact_report(final_report),
        })
        raise ValueError("Professional director blocked at final protected CTA gate.")
    projection = workspace.deliver()
    # Publish legacy B1.2 only when there is an approved whole AV plan,
    # never as a standalone text-only planning deliverable.
    _b12_checkpoints(output, source_beats, core)
    _save(root / "beats.json", {
        "source_sha256": source_sha,
        "narrative_core": core, "blocks": source_beats,
    })
    final_plan = workspace.candidate
    if final_plan is None or final_plan["narrative_core"] != core:
        raise ValueError("Professional planning changed the approved source core.")
    _save(root / "approved_plan.json", {
        "source_sha256": source_sha,
        "groups": materialized_groups, "plan": final_plan,
        "validation": final_report, "usage": usage,
    })
    _save(root / "editor_plan.json", projection["editor_plan"])
    _save(_phase_file(root, "05_delivery"), {
        "status": "passed", "source_sha256": source_sha,
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
    print("  [OpenAI] Cinco outputs de fases guardados y plan validado.",
          flush=True)
    return {
        "b11": b11, "groups": materialized_groups,
        "projection": projection, "usage": usage,
        "validation": final_report,
    }
