"""Scoped Python operations exposed as real SDK tools in production mode."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ai_video_factory.bots.agent_b.instructions import InstructionBundle
from ai_video_factory.bots.agent_b.project import AgentBProject, PlanRejected, StaleRevision
from ai_video_factory.bots.agent_b.validation import sequence_index


@dataclass
class ProductionToolService:
    project: AgentBProject
    instructions: InstructionBundle
    max_calls: int = 100
    calls: int = 0
    last_report: list[dict[str, Any]] = field(default_factory=list)

    def _guard(self) -> None:
        self.calls += 1
        if self.calls > self.max_calls:
            raise RuntimeError("Authorized tool-call budget exceeded.")

    @staticmethod
    def _result(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _errors(issues: list[Any]) -> list[dict[str, Any]]:
        # Source-text diagnostic values are capped, not mirrored in full.
        return [{"code": item.code, "message": item.message,
                 "block_id": item.block_id, "beat_ids": item.beat_ids,
                 "expected": str(item.expected)[:120], "actual": str(item.actual)[:120]}
                for item in issues[:40]]

    def read_script(self, block_id: int = 0, offset: int = 0, limit: int = 12000) -> str:
        self._guard()
        if offset < 0 or not 1 <= limit <= 20000:
            raise ValueError("Invalid script range.")
        blocks = self.project.blocks
        if block_id:
            blocks = tuple(block for block in blocks if block.block_id == block_id)
            if not blocks:
                raise ValueError("Unknown B1.1 block ID.")
        joined = "".join(block.text for block in blocks)
        return self._result({"status": "ok", "block_id": block_id or None,
                             "offset": offset, "text": joined[offset:offset + limit],
                             "next_offset": min(len(joined), offset + limit),
                             "total_characters": len(joined),
                             "narrative_core": self.project.core})

    def read_group(self, block_ids: list[int]) -> str:
        self._guard()
        if not block_ids or len(block_ids) > 12 or len(set(block_ids)) != len(block_ids):
            raise ValueError("Specify 1–12 distinct B1.1 block IDs.")
        selected = [block.model_dump(mode="json") for block in self.project.blocks
                    if block.block_id in block_ids]
        if len(selected) != len(block_ids):
            raise ValueError("One or more B1.1 blocks do not exist.")
        return self._result({"status": "ok", "blocks": selected})

    def read_instructions(self, document: str, offset: int = 0,
                          limit: int = 12000) -> str:
        self._guard()
        return self._result({"status": "ok", **self.instructions.get(document, offset, limit)})

    def read_project_status(self) -> str:
        self._guard()
        return self._result({"status": "ok", **self.project.status(),
                             "instruction_sha256": self.instructions.sha256})

    def read_project_context(self) -> str:
        self._guard()
        return self._result({"status": "ok", "narrative_core": self.project.core,
                             "blocks": [{"block_id": b.block_id, "type": b.type,
                                         "emotional_entry": b.emotional_entry,
                                         "emotional_exit": b.emotional_exit,
                                         "characters": len(b.text)}
                                        for b in self.project.blocks],
                             **self.project.status()})

    def validate_plan(self, plan_json: str) -> str:
        self._guard()
        if len(plan_json) > 1_000_000:
            raise ValueError("Plan payload exceeds the 1 MB tool limit.")
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError as exc:
            return self._result({"status": "invalid", "errors": [{"code": "JSON_PARSE",
                                 "message": str(exc)}]})
        errors = self.project.validate_plan(plan)
        self.last_report = self._errors(errors)
        return self._result({"status": "ok" if not errors else "invalid",
                             "valid": not errors, "error_count": len(errors),
                             "errors": self.last_report})

    def read_validation_report(self) -> str:
        self._guard()
        return self._result({"status": "ok", "errors": self.last_report})

    def manage_visual_sequence(self, sequence_id: str) -> str:
        self._guard()
        if not sequence_id.isalpha() or not sequence_id.isupper() or len(sequence_id) > 4:
            raise ValueError("Sequence must be an uppercase A–Z name.")
        return self._result({"status": "ok", "sequence_id": sequence_id,
                             "assets": self.project.status()["sequence_index"].get(
                                 sequence_id, [])})

    def save_visual_plan(self, plan_json: str, expected_revision: int) -> str:
        self._guard()
        if len(plan_json) > 1_000_000:
            raise ValueError("Plan payload exceeds the 1 MB tool limit.")
        try:
            parsed = json.loads(plan_json)
        except json.JSONDecodeError as exc:
            return self._result({"status": "invalid", "error": str(exc)})
        try:
            revision = self.project.save_visual_plan(parsed,
                                                     expected_revision=expected_revision)
        except PlanRejected as exc:
            self.last_report = self._errors(exc.issues)
            return self._result({"status": "invalid", "error_count": len(exc.issues),
                                 "errors": self.last_report})
        except StaleRevision as exc:
            return self._result({"status": "stale", "message": str(exc),
                                 "project": self.project.status()})
        self.last_report = []
        return self._result({"status": "saved", "revision": revision,
                             "sequences": list(sequence_index(parsed))})

    def save_block_draft(self, block_id: int, block_json: str,
                         expected_revision: int) -> str:
        """Stage beats and visuals together for one source block, without publishing them."""
        self._guard()
        if len(block_json) > 400_000:
            raise ValueError("Block draft exceeds the 400 KB limit.")
        try:
            block = json.loads(block_json)
            revision = self.project.save_block_draft(
                block_id, block, expected_revision=expected_revision)
        except json.JSONDecodeError as exc:
            return self._result({"status": "invalid", "error": str(exc)})
        except PlanRejected as exc:
            self.last_report = self._errors(exc.issues)
            return self._result({"status": "invalid", "error_count": len(exc.issues),
                                 "errors": self.last_report})
        except StaleRevision as exc:
            return self._result({"status": "stale", "message": str(exc),
                                 "project": self.project.status()})
        self.last_report = []
        return self._result({"status": "draft_saved", "revision": revision,
                             "drafted_block_ids": self.project.status()["drafted_block_ids"]})

    def finalize_drafts(self, expected_revision: int) -> str:
        """Assemble all saved block drafts into one validated final visual plan."""
        self._guard()
        try:
            revision = self.project.finalize_drafts(expected_revision=expected_revision)
        except PlanRejected as exc:
            self.last_report = self._errors(exc.issues)
            return self._result({"status": "invalid", "error_count": len(exc.issues),
                                 "errors": self.last_report})
        except StaleRevision as exc:
            return self._result({"status": "stale", "message": str(exc),
                                 "project": self.project.status()})
        self.last_report = []
        return self._result({"status": "saved", "revision": revision,
                             "sequences": list(self.project.status()["sequence_index"])})

    def sdk_tools(self) -> list[Any]:
        """Build SDK function tools bound to this project's service, not global state."""
        try:
            from agents import function_tool
        except ImportError as exc:
            raise RuntimeError("Install openai-agents to enable Agent B production mode.") from exc

        @function_tool
        def read_script(block_id: int = 0, offset: int = 0, limit: int = 12000) -> str:
            """Read exact original script text, optionally scoped to a B1.1 block/range."""
            return self.read_script(block_id, offset, limit)

        @function_tool
        def read_group(block_ids: list[int]) -> str:
            """Read complete immutable B1.1 blocks for one planning group."""
            return self.read_group(block_ids)

        @function_tool
        def read_project_context() -> str:
            """Read the script-wide narrative core, block metadata, and plan status."""
            return self.read_project_context()

        @function_tool
        def read_instructions(document: str, offset: int = 0, limit: int = 12000) -> str:
            """Consult an allowlisted, pinned English instruction or image prompting guide."""
            return self.read_instructions(document, offset, limit)

        @function_tool
        def read_project_status() -> str:
            """Read current revision and global visual-sequence index."""
            return self.read_project_status()

        @function_tool
        def validate_plan(plan_json: str) -> str:
            """Run the Python validator against a candidate complete audiovisual plan."""
            return self.validate_plan(plan_json)

        @function_tool
        def read_validation_report() -> str:
            """Inspect the last structured Python validation failures for this run."""
            return self.read_validation_report()

        @function_tool
        def manage_visual_sequence(sequence_id: str) -> str:
            """Inspect A/B/... membership derived from the saved audiovisual plan."""
            return self.manage_visual_sequence(sequence_id)

        @function_tool
        def save_visual_plan(plan_json: str, expected_revision: int) -> str:
            """Atomically save ONE complete audiovisual plan after Python validation."""
            return self.save_visual_plan(plan_json, expected_revision)

        @function_tool
        def save_block_draft(block_id: int, block_json: str, expected_revision: int) -> str:
            """Persist one block's combined beats and visuals for later global assembly."""
            return self.save_block_draft(block_id, block_json, expected_revision)

        @function_tool
        def finalize_drafts(expected_revision: int) -> str:
            """Validate and save ONE complete plan from all staged B1.1 blocks."""
            return self.finalize_drafts(expected_revision)

        return [read_script, read_group, read_project_context, read_instructions,
                read_project_status, validate_plan, read_validation_report,
                manage_visual_sequence, save_visual_plan, save_block_draft, finalize_drafts]
