from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "scripts" / "smoke" / "run_ltx25_a2v_smoke_controlled.ps1"
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 is not installed")


def _fake_checkout(
    tmp_path: Path, *, fail_at: str | None = None
) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "fake_repo"
    smoke = repo / "scripts" / "smoke"
    salad = repo / "scripts" / "salad"
    pipeline = repo / "scripts" / "pipeline"
    for directory in (smoke, salad, pipeline):
        directory.mkdir(parents=True)
    (smoke / WRAPPER.name).write_text(WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")

    # These scripts are intentionally inert: this test never contacts Salad,
    # PostgreSQL, R2 or any paid GPU.
    (smoke / "check_ltx25_python_source.py").write_text(
        "print('fake source verified')\n", encoding="utf-8"
    )
    (pipeline / "check_r2_ready.py").write_text(
        "print('fake R2 ready')\n", encoding="utf-8"
    )
    (smoke / "wait_salad_ltx25_ready.py").write_text(
        "import sys\nprint('fake worker ready')\nsys.exit("
        + ("1" if fail_at == "ready" else "0")
        + ")\n",
        encoding="utf-8",
    )
    (smoke / "submit_ltx25_a2v_smoke.py").write_text(
        "import sys\nprint('fake smoke')\nsys.exit("
        + ("1" if fail_at == "smoke" else "0")
        + ")\n",
        encoding="utf-8",
    )
    (salad / "manage_salad_worker.ps1").write_text(
        "[CmdletBinding()]\n"
        "param([string]$Action, [string]$Service, [int]$Replicas, "
        "[string]$EnvFile, [switch]$AllowBootstrappingInstance, "
        "[switch]$NonInteractive)\n"
        "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'events.txt') -Value $Action\n"
        + ("if ($Action -eq 'Stop') { throw 'fake stop failure' }\n"
           if fail_at == "stop" else ""),
        encoding="utf-8",
    )
    audio = repo / "monje.wav"
    image = repo / "monje.png"
    audio.write_bytes(b"fake-wav")
    image.write_bytes(b"fake-png")
    return smoke / WRAPPER.name, audio, image, salad / "events.txt"


def _run_fake(
    tmp_path: Path, *, fail_at: str | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, object], list[str]]:
    wrapper, audio, image, events = _fake_checkout(tmp_path, fail_at=fail_at)
    completed = subprocess.run(
        [
            PWSH or "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(wrapper),
            "-Audio",
            str(audio),
            "-AvatarImage",
            str(image),
            "-SegmentId",
            "unique-fake-test",
            "-OutputDir",
            str(tmp_path / "unique-output"),
            "-NonInteractive",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [
        line.split("LTX25_A2V_LIFECYCLE_METRICS ", maxsplit=1)[1]
        for line in completed.stdout.splitlines()
        if "LTX25_A2V_LIFECYCLE_METRICS " in line
    ]
    assert len(lines) == 1, completed.stdout + completed.stderr
    return completed, json.loads(lines[0]), events.read_text(encoding="utf-8").splitlines()


def test_completed_smoke_measures_whole_cycle_and_stops_gpu(tmp_path: Path) -> None:
    completed, metrics, events = _run_fake(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert events == ["Start", "Stop"]
    assert metrics["outcome"] == "succeeded"
    assert metrics["profile"] == "reference"
    assert metrics["segment_id"] == "unique-fake-test"
    assert metrics["schema_version"] == 1
    phases = (
        "preflight_seconds",
        "capacity_start_seconds",
        "readiness_seconds",
        "smoke_seconds",
        "cleanup_seconds",
    )
    assert all(isinstance(metrics[phase], (int, float)) for phase in phases)
    assert metrics["end_to_end_seconds"] >= sum(float(metrics[phase]) for phase in phases)


@pytest.mark.parametrize(
    ("fail_at", "expected_non_null", "expected_null"),
    [
        ("ready", "readiness_seconds", "smoke_seconds"),
        ("smoke", "smoke_seconds", None),
    ],
)
def test_failed_smoke_reports_partial_phase_and_always_stops(
    tmp_path: Path, fail_at: str, expected_non_null: str, expected_null: str | None
) -> None:
    completed, metrics, events = _run_fake(tmp_path, fail_at=fail_at)
    assert completed.returncode != 0
    assert events == ["Start", "Stop"]
    assert metrics["outcome"] == "failed"
    assert isinstance(metrics[expected_non_null], (int, float))
    if expected_null is not None:
        assert metrics[expected_null] is None
    assert isinstance(metrics["cleanup_seconds"], (int, float))
    assert isinstance(metrics["end_to_end_seconds"], (int, float))


def test_failed_cleanup_cannot_be_reported_as_success(tmp_path: Path) -> None:
    completed, metrics, events = _run_fake(tmp_path, fail_at="stop")
    assert completed.returncode != 0
    assert events == ["Start", "Stop"]
    assert metrics["outcome"] == "cleanup_failed"
    assert isinstance(metrics["cleanup_seconds"], (int, float))
    assert isinstance(metrics["end_to_end_seconds"], (int, float))
