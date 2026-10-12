"""One autonomous Agents SDK director; no paid media calls or legacy bot handoffs."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ai_video_factory.bots.agent_b.instructions import InstructionBundle
from ai_video_factory.bots.agent_b.project import AgentBProject, PlanRejected, StaleRevision
from ai_video_factory.bots.agent_b.tools import ProductionToolService
from ai_video_factory.bots.contracts import MaterializedBlock


class AgentBRunError(RuntimeError):
    """No validated frozen plan was produced within the bounded agent attempts."""


def _diagnostic(path: Path, report: dict[str, Any]) -> None:
    """Write a project-local, bounded report; never include credentials or full script."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agent_b_report_", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _attempt_to_finalize(project: AgentBProject) -> tuple[bool, list[dict[str, Any]]]:
    """The controller, never the model, decides when all drafts constitute a valid plan."""
    state = project.status()
    if state["status"] == "frozen":
        return True, []
    revision = state["revision"]
    if not project.has_saved_plan() and len(state["drafted_block_ids"]) == len(project.blocks):
        try:
            revision = project.finalize_drafts(expected_revision=revision)
        except PlanRejected as exc:
            return False, [issue.to_dict() for issue in exc.issues[:15]]
    if not project.has_saved_plan():
        missing = sorted(set(block.block_id for block in project.blocks)
                         - set(state["drafted_block_ids"]))
        return False, [{
            "code": "NO_COMPLETE_PLAN",
            "message": "Save the full plan, or draft every B1.1 block before finalization.",
            "missing_block_ids": missing,
        }]
    try:
        project.freeze(expected_revision=revision)
    except PlanRejected as exc:
        return False, [issue.to_dict() for issue in exc.issues[:15]]
    except StaleRevision as exc:
        return False, [{"code": "FREEZE_STATE", "message": str(exc)}]
    return True, []


async def run_agent_b(
    project_dir: Path,
    source_blocks: Sequence[MaterializedBlock],
    narrative_core: Mapping[str, Any],
    *,
    model: str,
    instructions_dir: Path | None = None,
    max_attempts: int = 3,
    max_turns: int = 60,
    max_tool_calls: int = 120,
    sdk: Any | None = None,
) -> dict[str, Any]:
    """Plan one video. Only a controller-validated frozen plan is returned."""
    if not model.strip() or not 1 <= max_attempts <= 10 or not 1 <= max_turns <= 200:
        raise ValueError("Invalid model or agent execution limits.")
    if not 1 <= max_tool_calls <= 500:
        raise ValueError("Invalid tool budget.")
    bundle = InstructionBundle.load(instructions_dir)
    project = AgentBProject(project_dir, source_blocks, narrative_core,
                            instruction_sha256=bundle.sha256)
    if project.status()["status"] == "frozen":
        return project.frozen_plan()
    try:
        sdk = sdk or importlib.import_module("agents")
    except ImportError as exc:
        raise AgentBRunError("Install openai-agents before running Agent B.") from exc

    tools = ProductionToolService(project, bundle, max_calls=max_tool_calls)
    agent = sdk.Agent(
        name="Agent B", model=model,
        instructions=bundle.starting_instructions(),
        tools=tools.sdk_tools(),
    )
    session = sdk.SQLiteSession(project.identity_sha256, str(project.root / "session.sqlite3"))
    report_path = project.root / "agent_b_attempts.json"
    history: list[dict[str, Any]] = []
    prompt = (
        "Use tools to direct the supplied narration without rewriting a single character. "
        "You MUST commit ONE validated visual plan, not just reply with an explanation. "
        "Read read_project_context, read_group and read_script first. "
        "For long scripts, call save_block_draft for each block, then finalize_drafts. "
        "Otherwise call validate_plan and save_visual_plan with the full plan JSON. "
        "All writes require the live revision from read_project_status. "
        "The beat IDs must be 1A, 1B, 2A, etc.; every beat requires beat_type, "
        "visual_type, prompt, video_prompt, reference and audio. "
        "A media_video beat needs BOTH a still starting-frame prompt and a motion prompt. "
        "If a non-CTA media_image beat has >=70 raw text characters, supply exactly "
        "two different visual_subshots. Logical visual chains use A1., A2., B1., B2. "
        "and may resume after other scenes. Do not send a final reply before a "
        "tool returns status=saved. The controller will freeze only validated output. "
        "Read the creative documents before making creative choices.\n"
        + json.dumps({
            "narrative_core": dict(narrative_core),
            "blocks": [{"block_id": block.block_id, "type": block.type,
                        "emotional_entry": block.emotional_entry,
                        "emotional_exit": block.emotional_exit,
                        "character_count": len(block.text)}
                       for block in source_blocks],
        }, ensure_ascii=False)
    )
    for attempt in range(1, max_attempts + 1):
        previous_calls = tools.calls
        try:
            result = await sdk.Runner.run(
                agent, prompt, session=session, max_turns=max_turns,
            )
        except Exception as exc:
            history.append({"attempt": attempt, "event": "sdk_error",
                            "exception_type": type(exc).__name__,
                            "tool_calls": tools.calls - previous_calls})
            _diagnostic(report_path, {"model": model, "status": "sdk_error",
                                      "attempts": history, "project": project.status()})
            raise AgentBRunError(
                f"Agent B SDK error ({type(exc).__name__}); see {report_path}."
            ) from exc

        accepted, errors = _attempt_to_finalize(project)
        last_output = str(getattr(result, "final_output", "") or "")
        entry = {
            "attempt": attempt,
            "event": "accepted" if accepted else "not_accepted",
            "tool_calls": tools.calls - previous_calls,
            "project_revision": project.status()["revision"],
            "drafted_block_ids": project.status()["drafted_block_ids"],
            "final_reply_preview": last_output[:300],
            "controller_errors": errors,
            "last_validation_errors": tools.last_report[:12],
        }
        history.append(entry)
        _diagnostic(report_path, {"model": model, "status": entry["event"],
                                  "attempts": history, "project": project.status()})
        if accepted:
            return project.frozen_plan()

        issues = errors or tools.last_report or [{
            "code": "NO_TOOL_DELIVERY",
            "message": "The model returned without calling a successful save tool.",
        }]
        prompt = (
            "Your last attempt was NOT accepted. The following are hard controller "
            "diagnostics, not a request to change creative direction. "
            "Call read_project_status and read_validation_report, repair the rejected "
            "fields, then commit the plan. If you staged all blocks, call finalize_drafts. "
            "If a draft is rejected, fix its exact fields and save that block again. "
            f"Attempt {attempt + 1}/{max_attempts}. Diagnostics: "
            + json.dumps(issues[:12], ensure_ascii=False, default=str)
            + ". Your prior response must not be treated as a saved plan."
        )
    reason = history[-1]["controller_errors"] or history[-1]["last_validation_errors"]
    codes = ", ".join(x.get("code", "?") for x in reason[:5]) or "NO_TOOL_DELIVERY"
    raise AgentBRunError(
        f"No valid unified audiovisual plan after {max_attempts} bounded agent "
        f"attempts ({codes}). Inspect {report_path} for tool and validator diagnostics."
    )
