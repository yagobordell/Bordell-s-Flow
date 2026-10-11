"""No-network integration contract for the *installed* OpenAI Agents SDK.

Mock-based unit tests cannot detect changed SDK constructors, response schemas,
tool registration or sqlite session implementations. Run this with the same
`uv --with openai-agents` overlay used by run.ps1.
"""
# ruff: noqa: S101
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agents import (
    Agent,
    ModelBehaviorError,
    ModelSettings,
    RunConfig,
    RunHooks,
    Runner,
    SQLiteSession,
    function_tool,
    set_default_openai_key,
)
from agents.agent_output import AgentOutputSchema
from agents.testing import ScriptedModel, assistant_message

from ai_video_factory.agents.professional.direction import (
    DirectionCorrection,
    WholeVideoDirection,
)
from ai_video_factory.agents.professional.runner import CreativeReview, GroupProposal
from ai_video_factory.agents.professional.tools import (
    ProfessionalPlanWorkspace,
    build_agents_sdk_tools,
)
from ai_video_factory.bots.contracts import B11Output


def check() -> None:
    for output_type in (
        GroupProposal, B11Output, WholeVideoDirection,
        CreativeReview, DirectionCorrection,
    ):
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
        model_settings=ModelSettings(parallel_tool_calls=False),
    )
    clone = agent.clone(output_type=WholeVideoDirection)
    assert clone.output_type is WholeVideoDirection
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
    class AuditHooks(RunHooks):
        def __init__(self) -> None:
            self.calls = 0

        async def on_llm_end(self, context, agent, response) -> None:
            self.calls += 1
            assert response.usage is not None

    proposal_json = json.dumps({
        "narrative_core": {
            "central_question": "Why?", "final_answer": "Because.",
        },
        "blocks": [{
            "block_id": 1, "type": "close",
            "emotional_entry": "open", "emotional_exit": "resolved",
            "span": {
                "first_words": "Inicio.", "last_words": "Final.",
            },
        }],
    })
    scripted = ScriptedModel([
        [assistant_message("{this-is-not-json")],
        [assistant_message(proposal_json)],
    ])
    live_parser = Agent(
        name="SDK structured JSON retry test (offline)",
        model=scripted, output_type=GroupProposal,
    )
    audit_hooks = AuditHooks()
    try:
        Runner.run_sync(
            live_parser, "Return the group proposal", max_turns=2,
            run_config=RunConfig(tracing_disabled=True), hooks=audit_hooks,
        )
    except ModelBehaviorError:
        pass
    else:
        raise RuntimeError("The SDK did not reject deliberately malformed JSON.")
    assert audit_hooks.calls == 1, "Malformed JSON must still emit on_llm_end"
    good = Runner.run_sync(
        live_parser, "Return the valid group proposal", max_turns=2,
        run_config=RunConfig(tracing_disabled=True),
    )
    assert good.final_output.blocks[0].block_id == 1
    scripted.assert_complete()

    joint_json = json.dumps({
        "blocks": [{
            "block_id": 1,
            "beats": [{
                "beat_id": "1A",
                "end_anchor": "Final.",
                "occurrence": 1,
                "beat_type": "return",
                "visual_type": "avatar",
                "prompt": None,
                "video_prompt": None,
                "reference": False,
                "audio": True,
                "image_prompts": None,
                "generation_mode": None,
                "editor_notes": [],
                "cta_group_id": None,
                "cta_reference_sources": None,
            }],
        }],
    })
    direction_model = ScriptedModel([[assistant_message(joint_json)]])
    joint_agent = Agent(
        name="SDK single call beats AND visuals offline",
        model=direction_model, output_type=WholeVideoDirection,
    )
    joint_result = Runner.run_sync(
        joint_agent, "Plan both beats and visuals together",
        max_turns=1, run_config=RunConfig(tracing_disabled=True),
    )
    assert joint_result.final_output.blocks[0].beats[0].end_anchor == "Final."
    assert joint_result.final_output.blocks[0].beats[0].visual_type == "avatar"
    direction_model.assert_complete()

    correction_model = ScriptedModel([
        [assistant_message(json.dumps({
            "replaced_groups": [], "visual_patches": [],
        }))],
    ])
    correction_agent = joint_agent.clone(
        model=correction_model, output_type=DirectionCorrection,
    )
    correction_result = Runner.run_sync(
        correction_agent, "No changes needed",
        max_turns=1, run_config=RunConfig(tracing_disabled=True),
    )
    assert correction_result.final_output.replaced_groups == []
    correction_model.assert_complete()
    print(
        "SDK real: strict joint beat+visual JSON and correction schemas, "
        "failure hooks and retry OK"
    )


if __name__ == "__main__":
    check()
