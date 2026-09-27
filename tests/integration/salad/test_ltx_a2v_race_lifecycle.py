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
IMAGE = "docker.io/example/worker@sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("failed_at", "expected_outcome", "expected_events"),
    [
        (None, "succeeded", ["Stop"]),
        ("race", "failed", ["Stop"]),
        ("smoke", "failed", ["Stop"]),
        ("reset", "cleanup_failed", ["Stop"]),
    ],
)
def test_two_gpu_race_stops_and_resets_before_reporting_success(
    tmp_path: Path,
    failed_at: str | None,
    expected_outcome: str,
    expected_events: list[str],
) -> None:
    repo = tmp_path / "fake_repo"
    smoke = repo / "scripts" / "smoke"
    salad = repo / "scripts" / "salad"
    pipeline = repo / "scripts" / "pipeline"
    for path in (smoke, salad, pipeline):
        path.mkdir(parents=True)
    (smoke / WRAPPER.name).write_text(WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
    (smoke / "check_ltx25_python_source.py").write_text(
        "print('fake source verified')\n", encoding="utf-8"
    )
    (pipeline / "check_r2_ready.py").write_text(
        "print('fake R2 ready')\n", encoding="utf-8"
    )
    (smoke / "start_ltx25_race_select.py").write_text(
        "import sys\n"
        "if '--reset-stopped' in sys.argv:\n"
        "    print('fake race reset')\n"
        f"    sys.exit({1 if failed_at == 'reset' else 0})\n"
        "print('fake two-to-one race')\n"
        f"sys.exit({1 if failed_at == 'race' else 0})\n",
        encoding="utf-8",
    )
    (smoke / "wait_salad_ltx25_ready.py").write_text(
        "print('fake one ready survivor')\n", encoding="utf-8"
    )
    (smoke / "submit_ltx25_a2v_smoke.py").write_text(
        "import sys\nprint('fake video')\n"
        f"sys.exit({1 if failed_at == 'smoke' else 0})\n",
        encoding="utf-8",
    )
    (salad / "check_salad_gpu_availability.ps1").write_text(
        "param([string]$Service, [string]$EnvFile)\n"
        "[pscustomobject]@{service='ltx25'; available_gpu_high=2}\n",
        encoding="utf-8",
    )
    (salad / "manage_salad_worker.ps1").write_text(
        "[CmdletBinding()]\n"
        "param([string]$Action, [string]$Service, [int]$Replicas, "
        "[string]$EnvFile, [switch]$AllowBootstrappingInstance, "
        "[switch]$NonInteractive)\n"
        "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'events.txt') -Value $Action\n",
        encoding="utf-8",
    )
    audio, avatar = repo / "monje.wav", repo / "monje.png"
    audio.write_bytes(b"fake-wav")
    avatar.write_bytes(b"fake-png")
    completed = subprocess.run(
        [
            PWSH or "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(smoke / WRAPPER.name),
            "-Audio",
            str(audio),
            "-AvatarImage",
            str(avatar),
            "-SegmentId",
            "unique-mock-race",
            "-StartupReplicas",
            "2",
            "-RaceTimeoutSeconds",
            "120",
            "-ExpectedPinnedImage",
            IMAGE,
            "-NonInteractive",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    metrics = [
        json.loads(line.split("LTX25_A2V_LIFECYCLE_METRICS ", maxsplit=1)[1])
        for line in completed.stdout.splitlines()
        if "LTX25_A2V_LIFECYCLE_METRICS " in line
    ]
    assert len(metrics) == 1, completed.stdout + completed.stderr
    assert metrics[0]["outcome"] == expected_outcome
    assert metrics[0]["startup_replicas"] == 2
    events = (salad / "events.txt").read_text(encoding="utf-8").splitlines()
    assert events == expected_events
    assert "fake race reset" in completed.stdout
    if failed_at is None:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert completed.stdout.index("fake one ready survivor") < completed.stdout.index(
            "fake video"
        )
    else:
        assert completed.returncode != 0
    if failed_at == "race":
        assert "fake video" not in completed.stdout
