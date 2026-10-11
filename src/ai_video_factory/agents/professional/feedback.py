"""Controlled long-term memory feedback for the Professional OpenAI director.

Feedback is proposed by the SAME SDK agent, not silently applied. Approving a
proposal appends only user-reviewed memory notes; immutable rules and phase
instructions are never rewritten by model-proposed text.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_video_factory.bots.contracts import StrictModel


class ClarificationProposal(StrictModel):
    file: str
    reason: str
    proposed_text: str


class FeedbackAnalysis(StrictModel):
    explanation: str
    groups_memory_additions: list[str]
    beats_visuals_memory_additions: list[str]
    instruction_clarifications: list[ClarificationProposal]


def _safe_read(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Feedback file missing or unsafe: {path}")
    return path.read_text(encoding="utf-8")


def parse_feedback_arg(argument: str) -> str:
    """Text by default; @path or an existing .md/.txt file means file content."""
    if argument.startswith("@"):
        return _safe_read(Path(argument[1:]))
    path = Path(argument)
    if path.suffix.lower() in {".md", ".txt"} and path.is_file():
        return _safe_read(path)
    return argument


async def propose_feedback(
    *,
    argument: str, output: Path, script: str, model: str,
    api_key: str | None = None,
) -> Path:
    """Make a pending, auditable proposal; do not change any active prompt."""
    from agents import Agent, Runner, set_default_openai_key

    if api_key:
        set_default_openai_key(api_key)
    text = parse_feedback_arg(argument)
    if not text.strip() or len(text) > 25_000:
        raise ValueError("Feedback must be nonempty and at most 25,000 characters.")
    previous = output / "agent_openai" / "approved_plan.json"
    if previous.is_symlink() or not previous.is_file():
        raise ValueError("Feedback requires a validated Professional agent plan.")
    saved = json.loads(previous.read_text(encoding="utf-8"))
    source_sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if saved.get("source_sha256") != source_sha:
        raise ValueError("Feedback script SHA differs from the original agent run.")
    from ai_video_factory.agents.professional.prompt_loader import load_phase_instructions

    agent = Agent(
        name="Bordell Professional Director",
        model=model,
        instructions=load_phase_instructions("04_correction") + """

FEEDBACK-ONLY TASK: propose long-term memory improvements from explicit user feedback.
Do NOT execute tools, rewrite the video, or change mandatory rules.
Two narrow memory lists may contain concise, durable preferences confirmed by
the user. Do not record a one-off scene fact as a permanent rule.
For any vague, conflicting or technically forbidden suggestion, put it in
instruction_clarifications with a precise rationale for human review.
Never claim to have edited any file. Empty suggestion lists are acceptable.
""",
        output_type=FeedbackAnalysis,
    )
    print("  [OpenAI feedback] Analizando comentario y posibles aclaraciones...", flush=True)
    result = await Runner.run(agent, json.dumps({
        "user_feedback": text,
        "source_script": script,
        "reviewed_visual_plan": saved.get("plan"),
        "current_validation": saved.get("validation"),
    }, ensure_ascii=False))
    analysis = result.final_output
    if not isinstance(analysis, FeedbackAnalysis):
        raise ValueError("Feedback agent returned no structured proposal.")

    folder = output / "agent_openai" / "feedback"
    if folder.is_symlink():
        raise ValueError("Unsafe feedback directory.")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("proposal_" + uuid4().hex + ".json")
    target.write_text(json.dumps({
        "schema_version": "professional-feedback-v1",
        "status": "pending_approval",
        "script_sha256": source_sha,
        "feedback": text,
        "analysis": analysis.model_dump(mode="json"),
        "usage": {
            "input_tokens": result.context_wrapper.usage.input_tokens,
            "output_tokens": result.context_wrapper.usage.output_tokens,
            "total_tokens": result.context_wrapper.usage.total_tokens,
        },
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        "  [OpenAI feedback] tokens: "
        f"entrada={result.context_wrapper.usage.input_tokens}, "
        f"salida={result.context_wrapper.usage.output_tokens}",
        flush=True,
    )
    return target


def approve_feedback(path: Path, *, output: Path, memory_root: Path) -> dict[str, Any]:
    """Apply strictly only the approved memory additions, never immutable rules."""
    directory = output / "agent_openai" / "feedback"
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("Unsafe feedback proposal.")
    location = path.resolve()
    if not location.is_relative_to(directory.resolve()) or not location.is_file():
        raise ValueError("Only a saved proposal from this video's feedback folder is allowed.")
    saved = json.loads(location.read_text(encoding="utf-8"))
    if (
        saved.get("schema_version") != "professional-feedback-v1"
        or saved.get("status") != "pending_approval"
    ):
        raise ValueError("Proposal is not pending approval.")
    analysis = FeedbackAnalysis.model_validate(saved["analysis"])
    target_files = (
        ("groups.md", analysis.groups_memory_additions),
        ("beats_visuals.md", analysis.beats_visuals_memory_additions),
    )
    if memory_root.is_symlink():
        raise ValueError("Unsafe Professional memory root.")
    memory_root.mkdir(parents=True, exist_ok=True)
    applied: dict[str, int] = {}
    for filename, notes in target_files:
        if any(not note.strip() or len(note) > 1000 for note in notes):
            raise ValueError("Approved memory note empty or excessively long.")
        destination = memory_root / filename
        if destination.is_symlink():
            raise ValueError("Unsafe memory file.")
        current = destination.read_text(encoding="utf-8") if destination.exists() else (
            "# Approved Professional preferences\n"
        )
        new = [f"- {note.strip()}" for note in notes if f"- {note.strip()}" not in current]
        if new:
            destination.write_text(
                current.rstrip() + "\n\n" + "\n".join(new) + "\n",
                encoding="utf-8",
            )
        applied[filename] = len(new)
    saved["status"] = "approved_memory_only"
    location.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "memory_additions": applied,
        "instruction_clarifications_for_manual_review": [
            c.model_dump() for c in analysis.instruction_clarifications
        ],
    }
