import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from ai_video_factory.agents.professional.feedback import _run_with_prompt_edit_tracking
from ai_video_factory.agents.professional.prompt_editing import (
    PromptInstructionEditor,
    build_prompt_instruction_tools,
)
from ai_video_factory.agents.professional.prompt_loader import operational_prompt_paths


def test_feedback_editor_exposes_only_live_instruction_files():
    paths = set(operational_prompt_paths())

    assert "phases/02_planning.md" in paths
    assert "phases/03_evaluation.md" in paths
    assert "phases/04_correction.md" in paths
    assert "rules/professional_cta.md" in paths
    assert not any(path.startswith("memory/") for path in paths)
    assert not any("source_snapshots" in path for path in paths)


def test_feedback_runner_disables_default_ten_turn_cap(tmp_path):
    seen = {}

    class FakeRunner:
        @staticmethod
        async def run(_agent, _payload, *, max_turns):
            seen["max_turns"] = max_turns
            return SimpleNamespace(final_output="done")

    result, changes, audit = asyncio.run(_run_with_prompt_edit_tracking(
        FakeRunner,
        object(),
        "feedback",
        editor=PromptInstructionEditor(tmp_path / "prompts"),
        output=tmp_path / "video",
    ))

    assert seen["max_turns"] is None
    assert result.final_output == "done"
    assert changes == []
    assert audit is None


def test_apply_patch_tool_edits_live_file_and_records_real_diff(tmp_path):
    pytest.importorskip("agents")
    from agents.editor import ApplyPatchOperation

    prompt_root = tmp_path / "prompts"
    target = prompt_root / "phases" / "03_evaluation.md"
    target.parent.mkdir(parents=True)
    target.write_text("## Editorial review\nFlag every issue.\n", encoding="utf-8")
    editor = PromptInstructionEditor(prompt_root)
    operation = ApplyPatchOperation(
        type="update_file",
        path="phases/03_evaluation.md",
        diff=(
            "@@\n"
            "-Flag every issue.\n"
            "+Separate fixable findings from protected code-contract conflicts.\n"
        ),
    )

    result = editor.update_file(operation)

    assert result.status == "completed"
    assert editor.read("phases/03_evaluation.md").replace("\r\n", "\n") == (
        "## Editorial review\n"
        "Separate fixable findings from protected code-contract conflicts.\n"
    )
    changes = editor.take_changes()
    assert changes[0]["file"] == "phases/03_evaluation.md"
    assert changes[0]["before_sha256"] != changes[0]["after_sha256"]
    assert "+Separate fixable findings" in changes[0]["diff"]


def test_failed_apply_patch_returns_error_to_agent_without_changing_file(tmp_path):
    pytest.importorskip("agents")
    from agents.editor import ApplyPatchOperation

    prompt_root = tmp_path / "prompts"
    target = prompt_root / "rules" / "visual_types.md"
    target.parent.mkdir(parents=True)
    original = "Use varied visuals.\n"
    target.write_text(original, encoding="utf-8")
    editor = PromptInstructionEditor(prompt_root)

    result = editor.update_file(ApplyPatchOperation(
        type="update_file",
        path="rules/visual_types.md",
        diff="@@\n-This line is stale.\n+Changed.\n",
    ))

    assert result.status == "failed"
    assert "Invalid Context" in (result.output or "")
    assert target.read_text(encoding="utf-8") == original
    assert editor.take_changes() == []


def test_editor_rejects_unapproved_paths_and_file_operations(tmp_path):
    pytest.importorskip("agents")
    from agents.editor import ApplyPatchOperation

    prompt_root = tmp_path / "prompts"
    prompt_root.mkdir()
    outside = tmp_path / "README.md"
    outside.write_text("Keep this file untouched.\n", encoding="utf-8")
    editor = PromptInstructionEditor(prompt_root)

    rejected_path = editor.update_file(ApplyPatchOperation(
        type="update_file",
        path="../README.md",
        diff="@@\n-Keep this file untouched.\n+Changed.\n",
    ))
    rejected_delete = editor.delete_file(ApplyPatchOperation(
        type="delete_file", path="rules/visual_types.md",
    ))

    assert rejected_path.status == "failed"
    assert rejected_delete.status == "failed"
    assert outside.read_text(encoding="utf-8") == "Keep this file untouched.\n"


def test_sdk_tools_include_live_reader_and_apply_patch(tmp_path):
    pytest.importorskip("agents")

    tools = build_prompt_instruction_tools(PromptInstructionEditor(tmp_path / "prompts"))

    assert [tool.name for tool in tools] == ["read_prompt_instruction", "apply_patch"]


def test_agents_sdk_runs_read_apply_verify_tool_loop(tmp_path):
    pytest.importorskip("agents")

    from agents import Agent, ModelSettings, RunConfig, Runner
    from agents.testing import ScriptedModel, assistant_message, function_call
    from openai.types.responses import ResponseApplyPatchToolCall
    from openai.types.responses.response_apply_patch_tool_call import OperationUpdateFile

    from ai_video_factory.agents.professional.feedback import FeedbackChatTurn

    prompt_root = tmp_path / "prompts"
    target = prompt_root / "phases" / "03_evaluation.md"
    target.parent.mkdir(parents=True)
    target.write_text("## Editorial review\nFlag every issue.\n", encoding="utf-8")
    editor = PromptInstructionEditor(prompt_root)
    stale_diff = "@@\n-This stale line is not present.\n+Change it.\n"
    diff = (
        "@@\n"
        "-Flag every issue.\n"
        "+Separate fixable findings from protected code-contract conflicts.\n"
    )
    model = ScriptedModel([
        [function_call(
            "read_prompt_instruction",
            {"file": "phases/03_evaluation.md"},
            call_id="read-prompt",
        )],
        [ResponseApplyPatchToolCall(
            id="apply-prompt",
            call_id="apply-prompt",
            type="apply_patch_call",
            status="in_progress",
            operation=OperationUpdateFile(
                type="update_file",
                path="phases/03_evaluation.md",
                diff=stale_diff,
            ),
        )],
        [function_call(
            "read_prompt_instruction",
            {"file": "phases/03_evaluation.md"},
            call_id="reread-after-failed-patch",
        )],
        [ResponseApplyPatchToolCall(
            id="apply-prompt-retry",
            call_id="apply-prompt-retry",
            type="apply_patch_call",
            status="in_progress",
            operation=OperationUpdateFile(
                type="update_file",
                path="phases/03_evaluation.md",
                diff=diff,
            ),
        )],
        [function_call(
            "read_prompt_instruction",
            {"file": "phases/03_evaluation.md"},
            call_id="verify-prompt",
        )],
        [assistant_message(json.dumps({
            "response": "Actualicé la regla y comprobé el archivo.",
            "conversation_summary": "Separar bloqueos corregibles de contratos Python.",
        }, ensure_ascii=False))],
    ])
    agent = Agent(
        name="Prompt edit integration test",
        model=model,
        instructions=(
            "Read the requested file, apply the requested change using apply_patch, "
            "read the file again, then return the structured response."
        ),
        tools=build_prompt_instruction_tools(editor),
        model_settings=ModelSettings(parallel_tool_calls=False),
        output_type=FeedbackChatTurn,
    )

    result = Runner.run_sync(
        agent,
        json.dumps({"editable_prompt_paths": ["phases/03_evaluation.md"]}),
        max_turns=7,
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output.response.startswith("Actualicé")
    assert "protected code-contract conflicts" in target.read_text(encoding="utf-8")
    assert editor.take_changes()[0]["file"] == "phases/03_evaluation.md"
    model.assert_complete()


def test_feedback_chat_edits_and_audits_md_in_same_turn(tmp_path, monkeypatch):
    pytest.importorskip("agents")

    from agents.testing import ScriptedModel, assistant_message, function_call
    from openai.types.responses import ResponseApplyPatchToolCall
    from openai.types.responses.response_apply_patch_tool_call import OperationUpdateFile

    from ai_video_factory.agents.professional import feedback, prompt_editing

    prompt_root = tmp_path / "prompts"
    target = prompt_root / "phases" / "03_evaluation.md"
    target.parent.mkdir(parents=True)
    target.write_text("## Editorial review\nFlag every issue.\n", encoding="utf-8")
    monkeypatch.setattr(prompt_editing, "_PROMPT_ROOT", prompt_root)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(messages))
    monkeypatch.setattr(feedback, "_load_feedback_context", lambda *_: {
        "execution_outcome": {"status": "failed"},
        "plan_source": "blocked_delivery_candidate",
        "reviewed_plan": {"blocks": []},
        "current_validation": {"status": "blocked", "summary": {}, "issues": []},
        "latest_phase_artifact": None,
        "correction_diagnostic": None,
    })
    messages = iter((
        "Has marcado todos los errores como corregibles; ajusta las instrucciones "
        "para distinguir conflictos de contrato.",
        "/salir",
    ))
    diff = (
        "@@\n"
        "-Flag every issue.\n"
        "+Separate fixable findings from protected code-contract conflicts.\n"
    )
    model = ScriptedModel([
        [function_call(
            "read_prompt_instruction",
            {"file": "phases/03_evaluation.md"},
            call_id="read-prompt",
        )],
        [ResponseApplyPatchToolCall(
            id="apply-prompt",
            call_id="apply-prompt",
            type="apply_patch_call",
            status="in_progress",
            operation=OperationUpdateFile(
                type="update_file",
                path="phases/03_evaluation.md",
                diff=diff,
            ),
        )],
        [function_call(
            "read_prompt_instruction",
            {"file": "phases/03_evaluation.md"},
            call_id="verify-prompt",
        )],
        [assistant_message(json.dumps({
            "response": "Actualicé las instrucciones y verifiqué el archivo.",
            "conversation_summary": "Distinguir error corregible de conflicto de contrato.",
        }, ensure_ascii=False))],
    ])
    script = "A saved script."
    asyncio.run(feedback.chat_feedback(
        output=tmp_path / "video",
        script=script,
        run_source_sha=hashlib.sha256(script.encode("utf-8")).hexdigest(),
        model=model,
    ))

    updated = target.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "Separate fixable findings" in updated
    audit_folder = tmp_path / "video" / "agent_openai" / "feedback" / "prompt_updates"
    audit_files = list(audit_folder.glob("*.json"))
    assert len(audit_files) == 1
    audit = json.loads(audit_files[0].read_text(encoding="utf-8"))
    assert audit["files"] == ["phases/03_evaluation.md"]
    assert audit["changes"][0]["after_sha256"] != audit["changes"][0]["before_sha256"]
    model.assert_complete()
