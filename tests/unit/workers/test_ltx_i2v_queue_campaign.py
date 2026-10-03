"""The I2V stress runner must stay a single-replica queue test, not claim GPU parallelism."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "scripts" / "smoke" / "ltx25_i2v_queue_campaign.py"
SPEC = importlib.util.spec_from_file_location("ltx25_i2v_queue_campaign", PATH)
assert SPEC and SPEC.loader
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)


def _arguments(tmp_path: Path) -> SimpleNamespace:
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    Image.new("RGB", (1280, 720), (35, 45, 55)).save(first)
    Image.new("RGB", (1280, 720), (45, 55, 65)).save(second)
    return SimpleNamespace(
        image_a=first,
        prompt_a="The presenter moves left.",
        image_b=second,
        prompt_b="The presenter moves right.",
        output_dir=tmp_path / "campaign",
        duration_seconds=5.0,
        seed_base=4242,
        timeout_seconds=10800,
    )


def test_queue_campaign_prepares_all_three_distinct_jobs_before_allocating_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _arguments(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        assert kwargs.get("check") is True
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(campaign.subprocess, "run", fake_run)
    campaign.prepare(args)
    plan = json.loads((args.output_dir / "campaign-plan.json").read_text(encoding="utf-8"))
    assert plan["purpose"].endswith("NOT parallel GPU inference")
    assert [entry["name"] for entry in plan["trials"]] == [
        "baseline-a", "queued-a", "queued-b"
    ]
    assert len({entry["seed"] for entry in plan["trials"]}) == 3
    assert len(calls) == 3
    assert all(command[2] == "prepare" for command in calls)
    with pytest.raises(ValueError, match="already exists"):
        campaign.prepare(args)


def test_queue_campaign_rejects_changed_inputs_before_dispatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _arguments(tmp_path)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **kw: None)
    campaign.prepare(args)
    args.prompt_b = "New and unapproved prompt"
    with pytest.raises(RuntimeError, match="Inputs changed"):
        campaign.run(args)


def test_queue_campaign_rejects_identical_scenarios(tmp_path: Path) -> None:
    args = _arguments(tmp_path)
    args.image_b = args.image_a
    args.prompt_b = args.prompt_a
    with pytest.raises(ValueError, match="distinct"):
        campaign.prepare(args)


def test_queue_campaign_separates_baseline_from_two_concurrent_submissions() -> None:
    planned = campaign.trials(4242)
    assert [entry["name"] for entry in planned] == [
        "baseline-a", "queued-a", "queued-b"
    ]
    assert [entry["seed"] for entry in planned] == [4242, 5242, 5243]
    assert "NOT two simultaneous diffusion" in campaign.__doc__
