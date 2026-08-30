from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "summarize_phase7_benchmarks.py"
    spec = importlib.util.spec_from_file_location("summarize_phase7_benchmarks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


summary = _load_script()


def _matrix(hardware: str, duration: float) -> dict:
    return {
        "schema_version": "1",
        "hardware_label": hardware,
        "workload": {"width": 768, "height": 1280, "num_frames": 121},
        "cases": [{"label": "bf16", "mean_duration_seconds": duration}],
    }


def test_comparison_selects_fastest_measured_case(tmp_path: Path) -> None:
    first = tmp_path / "l40s.json"
    second = tmp_path / "rtx4090.json"
    first.write_text("l40s", encoding="utf-8")
    second.write_text("rtx4090", encoding="utf-8")

    document = summary._comparison_document(
        [(first, _matrix("l40s", 30.0)), (second, _matrix("rtx4090", 20.0))]
    )

    assert document["fastest_case_by_mean_duration"] == {
        "hardware_label": "rtx4090",
        "label": "bf16",
    }
    assert len(document["source_matrices"]) == 2


def test_comparison_rejects_different_workloads(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    other = _matrix("rtx4090", 20.0)
    other["workload"]["num_frames"] = 97

    with pytest.raises(ValueError, match="Workload mismatch"):
        summary._comparison_document(
            [(first, _matrix("l40s", 30.0)), (second, other)]
        )
