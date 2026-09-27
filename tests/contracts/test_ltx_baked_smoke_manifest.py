"""Candidate Salad manifest selection keeps production resources untouched."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.smoke.wait_salad_ltx25_ready import _REPO_ROOT


def test_ltx_candidate_manifest_isolated_from_tracked_production_group(tmp_path: Path) -> None:
    tracked = Path("deploy/salad/services.json")
    original = tracked.read_bytes()
    candidate = json.loads(original)
    previous_group = candidate["services"]["ltx25"]["group_name"]
    candidate["services"]["ltx25"]["group_name"] = "ai-video-factory-ltx25-baked-validation-v1"
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")

    assert path.is_file()
    assert (
        json.loads(path.read_text(encoding="utf-8"))["services"]["ltx25"]["group_name"]
        != previous_group
    )
    assert tracked.read_bytes() == original
    assert (_REPO_ROOT / "deploy" / "salad" / "services.json").is_file()


def test_ltx_manager_and_controlled_smoke_support_the_same_candidate_manifest() -> None:
    manager = Path("scripts/salad/manage_salad_worker.ps1").read_text(encoding="utf-8")
    smoke = Path("scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1").read_text(
        encoding="utf-8"
    )
    ready = Path("scripts/smoke/wait_salad_ltx25_ready.py").read_text(encoding="utf-8")
    assert '[string]$ServicesPath = ""' in manager
    assert '$ServicesPath = [IO.Path]::GetFullPath($ServicesPath)' in manager
    assert '[string]$ServicesPath = ""' in smoke
    assert 'if ($ServicesPath) { $Stop["ServicesPath"] = $ServicesPath }' in smoke
    assert 'parser.add_argument("--services-path"' in ready
    assert 'services_path = args.services_path' in ready
