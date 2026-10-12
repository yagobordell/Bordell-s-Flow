"""One autonomous Agents SDK director; no paid media actions or legacy bot handoffs."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ai_video_factory.bots.agent_b.instructions import InstructionBundle
from ai_video_factory.bots.agent_b.project import AgentBProject, PlanRejected, StaleRevision
from ai_video_factory.bots.agent_b.tools import ProductionToolService
from ai_video_factory.bots.contracts import MaterializedBlock


class AgentBRunError(RuntimeError):
    """The agent did not deliver a valid, frozen plan within the allowed attempts."""


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
    """Plan a complete professional video and return its existing visual-plan shape.

    The SDK dependency and API credentials are used only when this is explicitly called.
    This function is not connected to Bordell's paid production pipeline yet.
    """
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
        raise AgentBRunError("Install openai-agents to run the production agent.") from exc
    tools = ProductionToolService(project, bundle, max_calls=max_tool_calls)
    agent = sdk.Agent(name="Agent B", model=model,
                      instructions=bundle.starting_instructions(), tools=tools.sdk_tools())
    session = sdk.SQLiteSession(project.identity_sha256, str(project.root / "session.sqlite3"))
    prompt = (
        "Plan the complete English narration as ONE professional audiovisual plan. "
        "Read the full source by calling read_script; inspect B1.1 metadata using "
        "read_project_context and read_group. Decide cut boundaries and visuals together, "
        "respecting suspense, viewer attention and logical A1/A2 continuity across blocks. "
        "Consult the creative and image-prompting references when relevant. "
        "For long scripts, save each combined beat/visual block with save_block_draft "
        "and call finalize_drafts; alternatively validate and save the complete plan. "
        "Use the current expected_revision for each write. "
        "Do not request human approval. "
        "Only the tool-saved plan is authoritative, not your final natural-language reply.\n"
        + json.dumps({"narrative_core": dict(narrative_core),
                      "blocks": [{"block_id": b.block_id, "type": b.type,
                                  "emotional_entry": b.emotional_entry,
                                  "emotional_exit": b.emotional_exit,
                                  "character_count": len(b.text)}
                                 for b in source_blocks]}, ensure_ascii=False)
    )
    for attempt in range(max_attempts):
        try:
            await sdk.Runner.run(agent, prompt, session=session, max_turns=max_turns)
        except Exception as exc:
            raise AgentBRunError("Agent SDK execution failed; no plan was accepted.") from exc
        state = project.status()
        if state["status"] == "planning" and state["revision"] > 0:
            try:
                project.freeze(expected_revision=state["revision"])
            except (PlanRejected, StaleRevision) as exc:
                prompt = ("Mandatory final gate rejected the candidate. Read validation "
                          "errors, repair the plan, run validate_plan and save_visual_plan "
                          f"again. Gate error: {exc}")
                continue
            return project.frozen_plan()
        prompt = (
            "No accepted complete plan was saved. Use the available Python tools "
            "to validate and save ONE full visual plan. Read the tool errors and repair "
            f"them. Retry {attempt + 1} of {max_attempts}. "
            f"Current project revision: {state['revision']}."
        )
    raise AgentBRunError(
        f"No valid unified audiovisual plan after {max_attempts} bounded agent attempts."
    )
