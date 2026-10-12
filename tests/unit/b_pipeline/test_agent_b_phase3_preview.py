"""Verify the checked-in Phase 3 grid fixture without credentials or media API calls."""

import json
from pathlib import Path

from ai_video_factory.bots.agent_b.validation import validate_unified_plan
from ai_video_factory.bots.contracts import MaterializedBlock
from scripts.pipeline.inspect_agent_b_grids import inspect_grid_plan

FIXTURE = Path(__file__).resolve().parents[3] / "examples" / "agent_b_grid_interleaved.json"


def test_interleaved_sample_passes_controller_and_offline_grid_inspection():
    plan = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sources = [
        MaterializedBlock(
            block_id=b["block_id"], type=b["type"],
            emotional_entry=b["emotional_entry"],
            emotional_exit=b["emotional_exit"],
            text="".join(beat["text"] for beat in b["beats"]),
        )
        for b in plan["blocks"]
    ]
    assert validate_unified_plan(plan, sources, plan["narrative_core"]) == []
    result = inspect_grid_plan(plan)
    assert result["status"] == "planned_offline"
    assert result["api_calls"] == 0
    assert result["physical_jobs"] == 3
    assert result["grids"] == 2
    assert [job["sequence"] for job in result["jobs"]] == ["A", "B", "A"]
    assert result["jobs"][2]["previous_block_id"] == 1
    assert result["jobs"][2]["previous_asset_id"] == "1A"
