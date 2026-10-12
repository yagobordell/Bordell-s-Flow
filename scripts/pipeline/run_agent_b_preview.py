"""Isolated Agent B planning preview and offline VS Code preflight."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import TypeAdapter

from ai_video_factory.bots.agent_b.instructions import InstructionBundle
from ai_video_factory.bots.agent_b.project import AgentBProject
from ai_video_factory.bots.agent_b.runner import run_agent_b
from ai_video_factory.bots.agent_b.tools import ProductionToolService
from ai_video_factory.bots.contracts import MaterializedBlock, NarrativeCore


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(prefix=".agent_b_plan_", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(scratch, path)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


def _parse_input(source_path: Path) -> tuple[list[MaterializedBlock], NarrativeCore]:
    data = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("B1.1 input must be a JSON object.")
    blocks = TypeAdapter(list[MaterializedBlock]).validate_python(data["blocks"])
    core = NarrativeCore.model_validate(data["narrative_core"])
    if not blocks:
        raise ValueError("At least one immutable B1.1 block is required.")
    identifiers = [block.block_id for block in blocks]
    if identifiers != sorted(set(identifiers)):
        raise ValueError("B1.1 blocks must have unique ascending block IDs.")
    return blocks, core


def preflight(project_dir: Path, blocks: list[MaterializedBlock],
              core: NarrativeCore, model: str) -> dict[str, Any]:
    """Verify installed *real* SDK/tools and project state; make zero API requests."""
    try:
        sdk = importlib.import_module("agents")
    except ImportError as exc:
        raise RuntimeError(
            "Agents SDK missing. Install config/agent-b-preview-requirements.txt "
            "into a dedicated preview virtual environment."
        ) from exc
    bundle = InstructionBundle.load()
    project = AgentBProject(project_dir, blocks, core.model_dump(mode="json"),
                            instruction_sha256=bundle.sha256)
    service = ProductionToolService(project, bundle)
    registered_tools = service.sdk_tools()
    required = {
        "read_script", "read_group", "read_project_context", "read_instructions",
        "read_project_status", "validate_plan", "read_validation_report",
        "manage_visual_sequence", "save_visual_plan", "save_block_draft",
        "finalize_drafts",
    }
    names = {tool.name for tool in registered_tools}
    if names != required:
        raise RuntimeError(f"Agent tool registry differs: {sorted(names ^ required)}")
    sdk.Agent(name="Agent B preflight", model=model,
              instructions=bundle.starting_instructions(), tools=registered_tools)
    sdk.SQLiteSession(project.identity_sha256, str(project.root / "session.sqlite3"))
    if json.loads(service.read_group([blocks[0].block_id]))["blocks"][0]["text"] != blocks[0].text:
        raise RuntimeError("The B1.1 read tool did not preserve exact source text.")
    status = project.status()
    return {
        "status": "preflight_ok",
        "api_calls": 0,
        "model": model,
        "block_count": len(blocks),
        "tool_count": len(registered_tools),
        "tool_names": sorted(names),
        "instruction_sha256": bundle.sha256,
        "project_status": status["status"],
        "project_revision": status["revision"],
        "project_dir": str(project_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="JSON with immutable materialized B1.1 blocks and narrative_core")
    parser.add_argument("--project", type=Path, required=True,
                        help="Private per-video checkpoint and SQLite session directory")
    parser.add_argument("--model", required=True, help="Supported OpenAI text model ID")
    parser.add_argument("--preflight", action="store_true",
                        help="Validate the real SDK, input, instructions and tools offline")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--max-tools", type=int, default=120)
    args = parser.parse_args()
    if not args.model.strip():
        parser.error("--model cannot be empty")
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    blocks, core = _parse_input(args.input)
    if args.preflight:
        print(json.dumps(preflight(args.project, blocks, core, args.model),
                         ensure_ascii=False, indent=2))
        return
    if not os.getenv("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY is missing. Put it in the ignored root .env file.")
    result = asyncio.run(run_agent_b(
        args.project, blocks, core.model_dump(mode="json"), model=args.model,
        max_attempts=args.max_attempts, max_turns=args.max_turns,
        max_tool_calls=args.max_tools,
    ))
    location = args.project / "visual_plan.json"
    _write_json(location, result)
    print(json.dumps({"status": "completed", "visual_plan": str(location),
                      "block_count": len(result["blocks"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
