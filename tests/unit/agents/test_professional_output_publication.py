"""Managed output publishing must expose every accepted agent phase artifact."""
from __future__ import annotations

import json

import pytest

from ai_video_factory.managed_layout import sync_managed_outputs
from ai_video_factory.publishing import PublicationError


def test_stopped_professional_agent_exposes_all_phase_outputs(tmp_path):
    work = tmp_path / ".video_factory_work" / "Enzo" / "test"
    script_folder = tmp_path / "data" / "Enzo" / "test"
    script_folder.mkdir(parents=True)
    (script_folder / "test.txt").write_text("El texto original.", encoding="utf-8")
    stage_names = (
        "01_groups", "02_beats", "02_visuals", "03_evaluation",
        "04_correction", "05_delivery",
    )
    for stage in stage_names:
        target = work / "agent_openai" / "phases" / stage / "output.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"phase": stage, "status": "passed"}), encoding="utf-8"
        )
    b11 = work / "B1.1" / "output.json"
    b11.parent.mkdir(parents=True)
    b11.write_text('{"pipeline_stage":"B1.1"}', encoding="utf-8")
    b12 = work / "B1.2" / "block_1" / "output.json"
    b12.parent.mkdir(parents=True)
    b12.write_text('{"pipeline_stage":"B1.2"}', encoding="utf-8")
    sync_managed_outputs(work, script_folder)
    for stage in stage_names:
        path = (
            script_folder / "outputs" / "agent_openai"
            / "phases" / stage / "output.json"
        )
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "phase": stage, "status": "passed",
        }
    assert (script_folder / "outputs" / "B1.1" / "output.json").is_file()
    assert (script_folder / "outputs" / "B1.2" / "block_1"
            / "output.json").is_file()
    # A repeated managed sync is safe and retains the source checkpoints.
    sync_managed_outputs(work, script_folder)
    assert b11.is_file()
    assert b12.is_file()


def test_symlinked_agent_artifacts_are_never_exposed(tmp_path):
    work = tmp_path / "work"
    script_folder = tmp_path / "data" / "Enzo" / "test"
    script_folder.mkdir(parents=True)
    (script_folder / "test.txt").write_text("El texto original.", encoding="utf-8")
    target = work / "agent_openai" / "phases" / "01_groups"
    target.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":"do-not-copy"}', encoding="utf-8")
    link = target / "output.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("This OS does not permit symlink creation.")
    with pytest.raises(PublicationError, match="Linked checkpoint asset"):
        sync_managed_outputs(work, script_folder)
    assert not (script_folder / "outputs" / "agent_openai").exists()
