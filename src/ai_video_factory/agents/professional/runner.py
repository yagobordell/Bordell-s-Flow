"""Five whole-script passes of ONE OpenAI Agents SDK director, Professional only.

A per-video session preserves continuity. Each phase receives an explicit source
copy and immutable rules. Only deterministic Python can release a plan. No
generation requests (images, Gemini, GPU) are dispatched from this module.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from ai_video_factory.agents.professional.prompt_loader import load_phase_instructions
from ai_video_factory.agents.professional.tools import (
    ProfessionalPlanWorkspace,
    build_agents_sdk_tools,
)
from ai_video_factory.bots.contracts import (
    B11Output,
    BeatType,
    BlockMeta,
    NarrativeCore,
    StrictModel,
    VisualType,
)


class GroupProposal(StrictModel):
    """Success-only B1.1 response: no redundant stage or error/null branches."""

    narrative_core: NarrativeCore
    blocks: list[BlockMeta] = Field(min_length=1)


class EditorialTrigger(StrictModel):
    text: str
    occurrence: int = Field(ge=1)


class EditorialNote(StrictModel):
    kind: str
    instruction: str | None
    trigger: EditorialTrigger | None


class PlannedBeat(StrictModel):
    beat_id: str
    text: str
    beat_type: BeatType
    visual_type: VisualType
    prompt: str | None
    video_prompt: str | None
    reference: bool
    audio: bool
    image_prompts: list[str] | None
    generation_mode: Literal["grid", "independent"] | None
    editor_notes: list[EditorialNote]
    cta_group_id: int | None
    cta_reference_sources: list[Literal["presenter", "ebook"]] | None


class PlannedBlock(StrictModel):
    block_id: int
    beats: list[PlannedBeat] = Field(min_length=1)


class PlannedVideo(StrictModel):
    visual_style: Literal["profesional"]
    narrative_core: NarrativeCore
    blocks: list[PlannedBlock] = Field(min_length=1)


def _wire_plan(plan: PlannedVideo) -> dict[str, Any]:
    """Preserve mandatory nullable B2 fields, omit absent optional metadata."""
    result = plan.model_dump(mode="json")
    for block in result["blocks"]:
        for beat in block["beats"]:
            for field in (
                "cta_group_id", "cta_reference_sources",
                "image_prompts", "generation_mode",
            ):
                if beat.get(field) is None:
                    beat.pop(field, None)
    return result


def _save(path: Path, payload: dict[str, Any]) -> None:
    """Checkpoint each phase; reject symlink and write via temporary replacement."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"Unsafe agent checkpoint: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    if temp.is_symlink():
        raise ValueError(f"Unsafe agent checkpoint temporary file: {temp}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _image_input(script: str, avatar: bytes, ebook: bytes | None) -> list[dict[str, Any]]:
    content: list[dict[str, str]] = [
        {"type": "input_text", "text": "AUTHORITY — FULL ORIGINAL SCRIPT:\n" + script},
        {"type": "input_text", "text": (
            "Use the following PNG only as presenter identity/reference. "
            "Never recreate its speaking room in generated B-roll."
        )},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64," + base64.b64encode(avatar).decode("ascii"),
        },
    ]
    if ebook is not None:
        content.extend([
            {"type": "input_text", "text": (
                "Authentic ebook cover. CTA beats must preserve its exact identity. "
                "Only create CTA visuals when actual ebook promotion occurs."
            )},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,"
                + base64.b64encode(ebook).decode("ascii"),
            },
        ])
    return [{"role": "user", "content": content}]


def _stats(phase: str, result: Any, seconds: float) -> dict[str, Any]:
    usage = result.context_wrapper.usage
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0
    report = {
        "phase": phase, "seconds": round(seconds, 3),
        "requests": usage.requests, "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens, "total_tokens": usage.total_tokens,
        "cached_input_tokens": cached,
    }
    print(
        f"  [OpenAI {phase}] {seconds:.1f}s | entrada={usage.input_tokens} "
        f"salida={usage.output_tokens} total={usage.total_tokens} "
        f"caché={cached} solicitudes={usage.requests}",
        flush=True,
    )
    return report


async def run_professional_director(
    script: str, *, output: Path, avatar: bytes, ebook: bytes | None,
    model: str, memory_root: Path | None = None, max_corrections: int = 2,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Run five global phases, validate after each, and save compatible outputs.

    Output is consumed by the *existing* downstream Professional TTS/image
    pipeline. A failed validation is a hard failure: no plan is published.
    """
    try:
        from agents import (
            Agent,
            ModelBehaviorError,
            ModelSettings,
            RunHooks,
            Runner,
            SQLiteSession,
            set_default_openai_key,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Missing OpenAI Agents SDK. Install openai-agents or use "
            "'uv run --with openai-agents --locked --extra dev'."
        ) from exc

    if api_key:
        set_default_openai_key(api_key)
    if not script.strip() or not avatar:
        raise ValueError("A complete raw script and an avatar PNG are mandatory.")
    if ebook is None:
        print(
            "  [OpenAI preflight] No hay portada real de ebook. "
            "CTA de ebook DESACTIVADO para este run. "
            "Si tu guion promociona un ebook, configura --ebook-cover "
            "antes de ejecutar la planificación.",
            flush=True,
        )
    if output.is_symlink() or output.is_junction():
        raise ValueError("Unsafe agent run directory.")
    root = output / "agent_openai"
    if root.is_symlink():
        raise ValueError("Unsafe agent checkpoint directory.")
    root.mkdir(parents=True, exist_ok=True)
    session = SQLiteSession("professional-" + uuid4().hex, str(root / "conversation.db"))
    workspace = ProfessionalPlanWorkspace(script=script, ebook_available=ebook is not None)
    tools = build_agents_sdk_tools(workspace)
    base = Agent(name="Bordell Professional Director", model=model)
    phases: list[dict[str, Any]] = []

    class UsageHooks(RunHooks):
        """Account for paid model calls even when strict JSON parsing fails."""

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
            usage = response.usage
            details = getattr(usage, "input_tokens_details", None)
            item = {
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
                "cached_input_tokens": int(getattr(details, "cached_tokens", 0) or 0),
                "response_id": getattr(response, "response_id", None),
                "request_id": getattr(response, "request_id", None),
            }
            self.calls.append(item)
            # Durable accounting, even if Runner raises while parsing this response.
            _save(root / "usage.json", {"phases": phases, "pending_calls": self.calls})

    async def execute(
        phase: str, input_data: str | list[dict[str, Any]],
        *, output_type: Any = str, use_tools: bool = False,
    ) -> Any:
        # Group anchors and whole-video plans are validated by Python AFTER
        # one structured model response. Avoid extra tool round trips for B1.1.
        enabled_tools = tools[2:] if use_tools and phase in {
            "03_evaluation", "04_correction", "05_delivery"
        } else []
        # No model write tool for the immense complete video plan: the
        # controller validates the single typed final output after each pass.
        actor = base.clone(
            instructions=load_phase_instructions(phase, memory_root=memory_root)
            + (
                "\n\nOUTPUT CONTRACT: The SDK enforces strict structured JSON. "
                "Return only the required schema object, never Markdown fences, "
                "comments or explanatory prose outside schema fields."
                if output_type is not str else ""
            ),
            output_type=output_type,
            tools=enabled_tools,
            model_settings=ModelSettings(
                max_tokens=8192 if phase == "01_groups" else 50000,
                parallel_tool_calls=False,
            ),
        )
        # Two bounded JSON repairs do not affect the semantic B1.1 validation
        # retry budget. No paid image/audio/video requests occur here.
        for attempt in range(3):
            print(
                f"\n  [OpenAI] Fase {phase} | llamada {attempt + 1}/3 ...",
                flush=True,
            )
            hooks = UsageHooks()
            started = perf_counter()
            try:
                repair = (
                    "La respuesta anterior no cumplió el JSON estricto del "
                    "SDK. Devuelve UNA salida íntegra conforme al esquema "
                    "sin bloques Markdown ni campos extra.\n\n"
                )
                if attempt == 0:
                    retry_prompt = input_data
                elif isinstance(input_data, str):
                    retry_prompt = repair + input_data
                else:
                    # Preserve original image attachments in every retry.
                    retry_prompt = copy.deepcopy(input_data)
                    retry_prompt[0]["content"].insert(
                        0, {"type": "input_text", "text": repair},
                    )
                result = await Runner.run(
                    actor, retry_prompt, session=session,
                    max_turns=1 if phase == "01_groups" else 18, hooks=hooks,
                )
            except ModelBehaviorError as exc:
                elapsed = perf_counter() - started
                calls = hooks.calls
                record = {
                    "phase": phase, "attempt": attempt + 1,
                    "status": "invalid_structured_output",
                    "error_type": type(exc).__name__,
                    "seconds": round(elapsed, 3), "calls": calls,
                    "requests": len(calls),
                    "input_tokens": sum(i["input_tokens"] for i in calls),
                    "output_tokens": sum(i["output_tokens"] for i in calls),
                    "total_tokens": sum(i["total_tokens"] for i in calls),
                }
                phases.append(record)
                _save(root / "usage.json", {"phases": phases})
                _save(root / f"{phase}_invalid_json_{attempt + 1}.json", record)
                print(
                    f"  [OpenAI {phase}] JSON inválido (intento {attempt + 1}/3); "
                    f"tokens facturados observados={record['total_tokens']}",
                    flush=True,
                )
                if attempt == 2:
                    raise RuntimeError(
                        f"{phase}: el SDK rechazó 3 respuestas estructuradas. "
                        f"Diagnóstico: {root / 'usage.json'}. No se inicia ninguna "
                        "generación posterior."
                    ) from None
                continue
            except Exception as exc:
                calls = hooks.calls
                record = {
                    "phase": phase, "attempt": attempt + 1,
                    "status": "failed", "error_type": type(exc).__name__,
                    "seconds": round(perf_counter() - started, 3),
                    "calls": calls, "requests": len(calls),
                    "input_tokens": sum(i["input_tokens"] for i in calls),
                    "output_tokens": sum(i["output_tokens"] for i in calls),
                    "total_tokens": sum(i["total_tokens"] for i in calls),
                }
                phases.append(record)
                _save(root / "usage.json", {"phases": phases})
                raise RuntimeError(
                    f"OpenAI phase {phase} failed ({type(exc).__name__}). "
                    f"Check {root / 'usage.json'}. No downstream media jobs started."
                ) from None
            metrics = _stats(phase, result, perf_counter() - started)
            metrics["attempt"] = attempt + 1
            metrics["status"] = "completed"
            phases.append(metrics)
            _save(root / "usage.json", {"phases": phases})
            return result.final_output
        raise RuntimeError("Unreachable structured-output retry state.")

    source_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
    input_script = "Guion completo (fuente literal inmutable):\n" + script

    # 1. B1.1 produces anchors only. Python materializes the original bytes.
    groups_ok = False
    check: dict[str, Any] = {"status": "blocked", "issues": []}
    for attempt in range(max_corrections + 1):
        proposal = await execute(
            "01_groups", input_script if attempt == 0 else (
                "Repara ÚNICAMENTE los anclajes B1.1 rechazados por Python. "
                + json.dumps(check, ensure_ascii=False) + "\n" + input_script
            ),
            output_type=GroupProposal,
        )
        b11 = B11Output(
            pipeline_stage="B1.1", narrative_core=proposal.narrative_core,
            blocks=proposal.blocks, error=None,
        )
        check = workspace.submit_groups([
            x.model_dump(mode="json") for x in proposal.blocks
        ])
        _save(root / f"groups_attempt_{attempt + 1}.json", {
            "output": b11.model_dump(mode="json"), "validation": check,
        })
        if check["status"] == "passed":
            groups_ok = True
            break
        print(
            "  [OpenAI 01_groups] Límite(s) rechazado(s) por Python: "
            + "; ".join(
                issue["rule_id"] + ": " + issue["message"]
                for issue in check["issues"][:4]
            ),
            flush=True,
        )
    if not groups_ok:
        raise ValueError("Professional grouping exhausted validation retries.")
    if b11.narrative_core is None:
        raise ValueError("B1.1 narrative core was not returned.")
    core = b11.narrative_core.model_dump(mode="json")
    workspace.expected_narrative_core = copy.deepcopy(core)
    _save(root / "groups.json", {
        "source_sha256": source_hash, "narrative_core": core,
        "blocks": check["groups"],
    })

    # 2. Full video, not a request per group. Input includes the original
    # avatar image and actual cover when present.
    phase2 = _image_input(script, avatar, ebook)
    if ebook is None:
        phase2[0]["content"].insert(0, {
            "type": "input_text",
            "text": (
                "RUN CAPABILITY GATE: An authentic ebook cover is NOT provided. "
                "This run CANNOT create ebook CTA beats. "
                "For EVERY beat, set cta_group_id=null and "
                "cta_reference_sources=null. "
                "Do not invent an ebook promotion, book cover, offer, price, "
                "QR overlay or ebook-related asset. "
                "If narration mentions a call to action, preserve the literal "
                "narration and illustrate it as ordinary non-ebook material. "
                "If the actual text explicitly promotes an ebook, the operator "
                "must configure a real ebook cover in a separate run; do not "
                "fabricate a replacement."
            ),
        })
    phase2[0]["content"].append({
        "type": "input_text",
        "text": "Materialized B1.1 groups and narrative_core (authoritative):\n"
        + json.dumps({
            "narrative_core": core,
            "groups": [
                {key: value for key, value in group.items() if key != "text"}
                for group in check["groups"]
            ],
        }, ensure_ascii=False),
    })
    plan = await execute("02_planning", phase2, output_type=PlannedVideo)
    workspace.submit_plan_revision(_wire_plan(plan))
    # The first accepted CTA ownership snapshot is frozen for all corrections:
    # a reviewer may not silently move or repaint an ebook promotion beat.
    if workspace.report is not None and workspace.report["status"] != "blocked":
        workspace.protected_cta_beats = {
            beat["beat_id"]: copy.deepcopy(beat)
            for block in workspace.candidate["blocks"]
            for beat in block["beats"]
            if beat.get("cta_group_id") is not None
        }
    # A changed narrative core is now a Python validation issue, so phase 4
    # can repair it before the delivery gate instead of wasting phase 2.
    # Missing cover is a deterministic, repairable plan validation error.
    # Never throw away the paid phase-2 output before saving its checkpoint.
    _save(root / "plan_initial.json", {
        "plan": workspace.candidate, "validation": workspace.report,
    })

    # 3. The model can actively call the bounded Python validation functions.
    assessment = await execute(
        "03_evaluation",
        "Evalúa el plan audiovisual completo; ejecuta las herramientas Python de "
        "validación y ritmo. No modifiques todavía la planificación. "
        "Informe determinista actual:\n"
        + json.dumps(workspace.validate_plan(), ensure_ascii=False),
        use_tools=True,
    )
    _save(root / "review.json", {
        "assessment": str(assessment), "validation": workspace.validate_plan(),
    })

    # 4. Cohesive revisions; each is followed by Python validation.
    for attempt in range(max_corrections):
        report = workspace.validate_plan()
        # The first correction pass is ALWAYS needed: Python cannot evaluate
        # causal fidelity, repetition or motion reliability.
        if attempt > 0 and report["status"] != "blocked":
            break
        corrected = await execute(
            "04_correction",
            "Revisa el plan completo, aplica las correcciones necesarias y "
            "devuelve la versión corregida íntegra. Guion completo:\n"
            + script + "\nGrupos:\n"
            + json.dumps(check["groups"], ensure_ascii=False)
            + "\nPlan completo:\n"
            + json.dumps(workspace.candidate, ensure_ascii=False)
            + "\nInforme Python:\n" + json.dumps(report, ensure_ascii=False)
            + "\nRevisión creativa del agente:\n" + str(assessment),
            output_type=PlannedVideo, use_tools=True,
        )
        workspace.submit_plan_revision(
            _wire_plan(corrected)
        )
        _save(root / f"correction_{attempt + 1}.json", {
            "plan": workspace.candidate, "validation": workspace.validate_plan(),
        })

    # 5. Final fresh check (in the controller, independent of model claims).
    report = workspace.validate_plan()
    if report["status"] == "blocked":
        _save(root / "blocked.json", {"validation": report})
        raise ValueError("Professional director cannot deliver: " +
                         ", ".join(i["rule_id"] for i in report["issues"]))
    if workspace.candidate is None or workspace.candidate["narrative_core"] != core:
        raise ValueError("Final Professional narrative_core is inconsistent with B1.1.")
    # CTA groups must be contiguous, chronologically ordered and nonoverlapping.
    cta_ids = [
        (beat["beat_id"], beat.get("cta_group_id"))
        for block in workspace.candidate["blocks"] for beat in block["beats"]
    ]
    groups_seen: set[int] = set()
    previous: int | None = None
    for beat_id, group_id in cta_ids:
        if group_id is not None and (
            type(group_id) is not int or group_id < 1
            or (group_id in groups_seen and group_id != previous)
        ):
            raise ValueError(f"Invalid CTA group/ordering at {beat_id}.")
        if group_id != previous:
            if previous is not None:
                groups_seen.add(previous)
        previous = group_id
    if previous is not None:
        groups_seen.add(previous)
    if groups_seen and groups_seen != set(range(1, len(groups_seen) + 1)):
        raise ValueError("CTA groups must have consecutive IDs starting at 1.")

    await execute(
        "05_delivery",
        "Confirma la entrega, sin modificar el plan. Ejecuta el validador y "
        "no declares válido nada que Python rechace. "
        + json.dumps(report["summary"], ensure_ascii=False),
        use_tools=True,
    )
    projection = workspace.deliver()
    _save(root / "approved_plan.json", {
        "source_sha256": source_hash,
        "groups": check["groups"], "plan": workspace.candidate,
        "validation": report, "usage": phases,
    })
    _save(root / "editor_plan.json", projection["editor_plan"])
    return {
        "b11": b11,
        "groups": check["groups"],
        "projection": projection,
        "usage": phases,
        "validation": report,
    }
