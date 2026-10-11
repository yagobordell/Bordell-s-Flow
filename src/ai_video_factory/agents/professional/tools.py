"""Bounded Python validator tools for one whole-script Professional agent session.

No arbitrary shell/Python execution and no writes to pipeline production paths.
An SDK runner may bind the closures returned by build_agents_sdk_tools().
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from ai_video_factory.agents.professional.compat import project_professional_plan
from ai_video_factory.agents.professional.validation import validate_groups, validate_plan


@dataclass
class ProfessionalPlanWorkspace:
    script: str
    ebook_available: bool = True
    expected_narrative_core: dict[str, Any] | None = None
    groups: list[dict[str, Any]] = field(default_factory=list)
    candidate: dict[str, Any] | None = None
    protected_cta_beats: dict[str, dict[str, Any]] = field(default_factory=dict)
    revision: int = 0
    report: dict[str, Any] | None = None

    def submit_groups(self, groups: list[dict[str, Any]]) -> dict[str, Any]:
        checked = validate_groups(self.script, groups)
        if checked["status"] == "passed":
            self.groups = copy.deepcopy(groups)
            self.candidate = None
            self.report = None
            self.revision = 0
        return checked

    def submit_plan_revision(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not self.groups:
            return {"status": "blocked", "message": "Groups must pass validation first."}
        self.candidate = copy.deepcopy(plan)
        self.revision += 1
        self.report = self.validate_plan()
        return {"revision": self.revision, **self.report}

    def validate_plan(self) -> dict[str, Any]:
        if self.candidate is None:
            return {"status": "blocked", "issues": [{
                "rule_id": "NO_CANDIDATE", "severity": "error",
                "message": "No candidate plan has been submitted.",
            }]}
        self.report = validate_plan(
            self.script, self.groups, self.candidate,
            protected_cta_beats=self.protected_cta_beats,
            ebook_available=self.ebook_available,
            expected_narrative_core=self.expected_narrative_core,
        )
        if self.report["status"] != "blocked":
            # Hard gate also includes ALL legacy downstream contracts and the
            # image-grid planner. A green narrative validator alone is unsafe.
            from ai_video_factory.providers.image_grids import GridPlanError

            try:
                project_professional_plan(
                    self.script, self.groups, self.candidate,
                    protected_cta_beats=self.protected_cta_beats,
                )
            except (ValueError, TypeError, GridPlanError) as exc:
                self.report["issues"].append({
                    "rule_id": "DOWNSTREAM_PREFLIGHT",
                    "severity": "error",
                    "message": str(exc),
                    "block_id": None,
                    "beat_ids": [],
                    "details": {},
                })
                self.report["status"] = "blocked"
        return copy.deepcopy(self.report)

    def get_rhythm_map(self) -> dict[str, Any]:
        report = self.validate_plan()
        return copy.deepcopy(report.get("rhythm_map") or {})

    def get_validation_issues(self) -> list[dict[str, Any]]:
        return self.validate_plan().get("issues", [])

    def deliver(self) -> dict[str, dict[str, Any]]:
        """Fail closed and revalidate the exact current candidate before delivery."""
        report = self.validate_plan()
        if report["status"] == "blocked":
            raise ValueError("Cannot deliver: final validator is not green.")
        if self.candidate is None:
            raise ValueError("Cannot deliver without a current candidate.")
        return project_professional_plan(
            self.script, self.groups, self.candidate,
            protected_cta_beats=self.protected_cta_beats,
        )


def build_agents_sdk_tools(workspace: ProfessionalPlanWorkspace) -> list[Any]:
    """Expose only approved validation operations via the official Agents SDK.

    The SDK dependency is imported lazily: the untouched Casero runtime and
    existing B pipeline do not need openai-agents installed.
    """
    from agents import function_tool

    @function_tool
    def submit_groups(groups_json: str) -> dict[str, Any]:
        """Validate B1.1-style group anchors from a JSON array."""
        try:
            groups = json.loads(groups_json)
            report = workspace.submit_groups(groups)
            # Materialized text stays controller-owned, not echoed as a giant
            # tool response that would exhaust the model's context.
            return {
                "status": report["status"], "issues": report["issues"],
                "block_count": len(report.get("groups", [])),
                "source_sha256": report.get("source_sha256"),
            }
        except (ValueError, TypeError) as exc:
            return {"status": "blocked", "message": str(exc)}

    @function_tool
    def submit_plan_revision(plan_json: str) -> dict[str, Any]:
        """Register and validate a whole-video Professional plan JSON object."""
        if len(plan_json) > 2_000_000:
            return {"status": "blocked", "message": "Plan exceeds tool input limit."}
        try:
            return workspace.submit_plan_revision(json.loads(plan_json))
        except (ValueError, TypeError) as exc:
            return {"status": "blocked", "message": str(exc)}

    @function_tool
    def validate_current_plan() -> dict[str, Any]:
        """Run every deterministic validation on the current revision."""
        return workspace.validate_plan()

    @function_tool
    def get_rhythm_map() -> dict[str, Any]:
        """Return the complete script-relative rhythm map (not actual timings)."""
        return workspace.get_rhythm_map()

    @function_tool
    def get_validation_issues() -> list[dict[str, Any]]:
        """Return blocking errors and editorial warnings for the current plan."""
        return workspace.get_validation_issues()

    return [
        submit_groups, submit_plan_revision, validate_current_plan,
        get_rhythm_map, get_validation_issues,
    ]
