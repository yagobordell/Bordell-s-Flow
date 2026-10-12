"""Project-scoped, revisioned Agent B plan store (no global narrative memory)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ai_video_factory.bots.agent_b.validation import (\n    PlanIssue,\n    sequence_index,\n    validate_unified_plan,\n)
from ai_video_factory.bots.contracts import MaterializedBlock


class PlanRejected(ValueError):
    """Rejected candidate; issues are safe to return to the agent in English."""

    def __init__(self, issues: list[PlanIssue]) -> None:
        self.issues = issues
        super().__init__(f"Plan rejected: {len(issues)} validation errors")


class StaleRevision(ValueError):
    """The caller tried to overwrite a newer project plan."""


class ProjectIdentityMismatch(ValueError):
    """The directory belongs to a different script or instruction version."""


def _canonical(value: Any) -> bytes:
    return json.dumps(\n        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")\n    ).encode("utf-8")


class AgentBProject:
    """Bind a private project directory to frozen B1.1 and one instruction bundle.

    This initial implementation assumes the controller serializes all writes to one
    project. Multi-process locking is required before concurrent writers are enabled.
    """

    def __init__(
        self,
        root: Path,
        source_blocks: Sequence[MaterializedBlock],
        narrative_core: Mapping[str, Any],
        *,
        instruction_sha256: str,
    ) -> None:
        self.root = Path(root)
        self.blocks = tuple(source_blocks)
        self.core = dict(narrative_core)
        self.instruction_sha256 = instruction_sha256
        identity = {"blocks": [b.model_dump(mode="json") for b in self.blocks],
                    "narrative_core": self.core, "instructions": instruction_sha256}
        self.identity_sha256 = hashlib.sha256(_canonical(identity)).hexdigest()
        self._path = self.root / "agent_b_project.json"
        self.root.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._check(self._read())
        else:
            self._write({"identity_sha256": self.identity_sha256, "revision": 0,
                         "status": "planning", "plan": None, "sequence_index": {}})

    def _read(self) -> dict[str, Any]:
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _check(self, state: dict[str, Any]) -> None:
        if state.get("identity_sha256") != self.identity_sha256:
            raise ProjectIdentityMismatch("Project script or instruction bundle changed.")

    def _write(self, state: dict[str, Any]) -> None:
        # Same-directory replace: readers never observe a half-written JSON file.
        fd, temporary = tempfile.mkstemp(prefix=".agent_b_", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def status(self) -> dict[str, Any]:
        state = self._read()
        self._check(state)
        return {"revision": state["revision"], "status": state["status"],
                "sequence_index": state["sequence_index"]}

    def validate_plan(self, plan: Mapping[str, Any]) -> list[PlanIssue]:
        return validate_unified_plan(plan, self.blocks, self.core)

    def save_visual_plan(self, plan: Mapping[str, Any], *, expected_revision: int) -> int:
        state = self._read()
        self._check(state)
        if state["revision"] != expected_revision:
            raise StaleRevision(f"Expected revision {expected_revision}; got {state['revision']}.")
        if state["status"] != "planning":
            raise StaleRevision("The accepted audiovisual plan has been frozen.")
        issues = self.validate_plan(plan)
        if issues:
            raise PlanRejected(issues)
        updated = {**state, "revision": expected_revision + 1,
                   "plan": dict(plan), "sequence_index": sequence_index(plan)}
        self._write(updated)
        return updated["revision"]

    def freeze(self, *, expected_revision: int) -> None:
        state = self._read()
        self._check(state)
        if state["revision"] != expected_revision or state["plan"] is None:
            raise StaleRevision("No matching validated plan to freeze.")
        if state["status"] != "planning":
            raise StaleRevision("The audiovisual plan is already frozen.")
        # Revalidate at the controller's mandatory acceptance gate.
        issues = self.validate_plan(state["plan"])
        if issues:
            raise PlanRejected(issues)
        self._write({**state, "status": "frozen", "revision": expected_revision + 1})

    def frozen_plan(self) -> dict[str, Any]:
        state = self._read()
        self._check(state)
        if state["status"] != "frozen" or state["plan"] is None:
            raise StaleRevision("No accepted frozen audiovisual plan exists.")
        return state["plan"]
