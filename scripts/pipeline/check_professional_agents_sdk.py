"""No-network integration contract for the *installed* OpenAI Agents SDK.

Mock-based unit tests cannot detect changed SDK constructors, response schemas,
tool registration or sqlite session implementations. Run this with the same
`uv --with openai-agents` overlay used by run.ps1.
"""
# ruff: noqa: S101
from __future__ import annotations

import tempfile
from pathlib import Path

from agents import (
    Agent,
    ModelBehaviorError,
    ModelSettings,
    RunHooks,
    SQLiteSession,
    function_tool,
    set_default_openai_key,
)
from agents.agent_output import AgentOutputSchema

from ai_video_factory.agents.professional.runner import PlannedVideo
from ai_video_factory.agents.professional.tools import (
    ProfessionalPlanWorkspace,
    build_agents_sdk_tools,
)
from ai_video_factory.bots.contracts import B11Output


def check() -> None:
    for output_type in (B11Output, PlannedVideo):
        schema = AgentOutputSchema(output_type)
        assert schema.is_strict_json_schema()
        body = schema.json_schema()
        assert body["type"] == "object"
        assert body["additionalProperties"] is False

    @function_tool
    def check_source() -> str:
        """Return a predictable test-only string."""
        return "ok"

    agent = Agent(
        name="SDK compatibility verification (offline)",
        model="gpt-6-luna",
        output_type=B11Output,
        tools=[check_source],
        model_settings=ModelSettings(max_tokens=16000, parallel_tool_calls=False),
    )
    clone = agent.clone(output_type=PlannedVideo)
    assert clone.output_type is PlannedVideo
    assert clone.model == agent.model
    assert issubclass(ModelBehaviorError, Exception)
    assert callable(getattr(RunHooks(), "on_llm_end", None))
    assert callable(set_default_openai_key)

    workspace = ProfessionalPlanWorkspace(script="Un comienzo. Y un final.")
    tools = build_agents_sdk_tools(workspace)
    assert [tool.name for tool in tools] == [
        "submit_groups", "submit_plan_revision", "validate_current_plan",
        "get_rhythm_map", "get_validation_issues",
    ]
    with tempfile.TemporaryDirectory() as temp:
        session = SQLiteSession("sdk-test", str(Path(temp) / "sdk.sqlite"))
        assert session is not None
    print("SDK real: strict schemas, Agent.clone, SDK tools, hooks, sqlite OK")


if __name__ == "__main__":
    check()
