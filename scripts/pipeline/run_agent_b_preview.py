"""Standalone Agent B planning preview. No voice, image, LTX or paid rendering jobs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from ai_video_factory.bots.agent_b.runner import run_agent_b
from ai_video_factory.bots.contracts import MaterializedBlock, NarrativeCore


def _write_json(path: Path, value: dict[str, object]) -> None:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="JSON containing exact materialized B1.1 blocks and narrative_core")
    parser.add_argument("--project", type=Path, required=True,
                        help="Private project directory for checkpoints and SQLite session")
    parser.add_argument("--model", required=True, help="Explicit OpenAI agent model")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--max-tools", type=int, default=120)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    blocks = TypeAdapter(list[MaterializedBlock]).validate_python(data["blocks"])
    core = NarrativeCore.model_validate(data["narrative_core"])
    result = asyncio.run(run_agent_b(
        args.project, blocks, core.model_dump(mode="json"), model=args.model,
        max_attempts=args.max_attempts, max_turns=args.max_turns,
        max_tool_calls=args.max_tools,
    ))
    location = args.project / "visual_plan.json"
    _write_json(location, result)
    print(json.dumps({"status": "completed", "visual_plan": str(location),
                      "block_count": len(result["blocks"])}))


if __name__ == "__main__":
    main()
