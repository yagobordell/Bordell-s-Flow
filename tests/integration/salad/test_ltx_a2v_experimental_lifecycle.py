"""Offline PowerShell end-to-end contract for the isolated A2V benchmark runner."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = _ROOT / "scripts" / "smoke" / "run_ltx25_a2v_benchmark_batch.ps1"
_PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(_PWSH is None, reason="PowerShell 7 is not installed")
_PIN = "registry.example.com/a2v@sha256:" + ("a" * 64)


def _create_fake_checkout(tmp_path: Path, *, fail_ready: bool) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    smoke = root / "scripts" / "smoke"
    salad = root / "scripts" / "salad"
    pipeline = root / "scripts" / "pipeline"
    source = root / "src" / "ai_video_factory" / "workers" / "ltx25"
    for folder in (smoke, salad, pipeline, source):
        folder.mkdir(parents=True, exist_ok=True)
    (smoke / _WRAPPER.name).write_text(_WRAPPER.read_text(encoding="utf-8"))
    (source / "__init__.py").write_text("")
    for file in (
        "check_ltx25_python_source.py",
        "check_ltx_a2v_benchmark_isolation.py",
        "check_ltx_a2v_benchmark_jobs.py",
    ):
        (smoke / file).write_text("print('fake preflight passed')\n")
    (pipeline / "check_r2_ready.py").write_text("print('fake R2 OK')\n")
    (smoke / "wait_salad_ltx25_ready.py").write_text(
        "import sys\nprint('fake /ready')\nsys.exit("
        + ("1" if fail_ready else "0")
        + ")\n"
    )
    (smoke / "submit_ltx25_a2v_smoke.py").write_text(
        "import json, pathlib, sys\n"
        "a = sys.argv\n"
        "out = pathlib.Path(a[a.index('--output-dir') + 1])\n"
        "profile = a[a.index('--profile') + 1]\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'avatar_segment.mp4').write_bytes(b'mock mp4')\n"
        "iteration = int(out.name.rsplit('-', 1)[-1])\n"
        "(out / 'metadata.json').write_text(json.dumps({"
        "'pipeline_reused': iteration > 1, 'prompt_cache_hit': iteration > 1, "
        "'total_elapsed_seconds': 60.0,"
        "'cuda_peak_reserved_mb': 24000.0, 'profile': profile"
        "}))\n"
    )
    (salad / "manage_salad_worker.ps1").write_text(
        "[CmdletBinding()]\n"
        "param([string]$Action, [string]$Service, [int]$Replicas, "
        "[string]$EnvFile, [switch]$AllowBootstrappingInstance, "
        "[switch]$NonInteractive)\n"
        "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'events.txt') "
        "-Value \"${Action}:$Service\"\n"
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.name=Test",
            "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-qm", "fake initial commit",
        ],
        check=True,
    )
    (root / "monje.wav").write_bytes(b"fake WAV")
    (root / "monje.png").write_bytes(b"fake PNG")
    return root, salad / "events.txt"


@pytest.mark.parametrize("fail_ready", [False, True])
def test_isolated_runner_never_mutates_production_or_i2v(
    tmp_path: Path, fail_ready: bool
) -> None:
    root, events = _create_fake_checkout(tmp_path, fail_ready=fail_ready)
    output = tmp_path / "benchmark-output"
    completed = subprocess.run(
        [
            _PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(root / "scripts" / "smoke" / _WRAPPER.name),
            "-Audio", str(root / "monje.wav"),
            "-AvatarImage", str(root / "monje.png"),
            "-Prompt", "The unchanged avatar speaks.",
            "-ExpectedPinnedImage", _PIN,
            "-Profiles", "reference-bench-cpu",
            "-Repetitions", "3",
            "-OutputDir", str(output), "-NonInteractive",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert events.exists(), completed.stdout + completed.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "Start:ltx25_a2v_experimental",
        "Stop:ltx25_a2v_experimental",
    ]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["pinned_image"] == _PIN
    assert manifest["prompt"] == "The unchanged avatar speaks."
    if fail_ready:
        assert completed.returncode != 0
        assert manifest["runs"] == []
    else:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert len(manifest["runs"]) == 3
        assert [row["iteration"] for row in manifest["runs"]] == [1, 2, 3]
        assert all(Path(row["metadata"]).is_file() for row in manifest["runs"])


def test_benchmark_budget_skips_repetitions_but_always_stops(
    tmp_path: Path,
) -> None:
    root, events = _create_fake_checkout(tmp_path, fail_ready=False)
    output = tmp_path / "budget"
    completed = subprocess.run(
        [
            _PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(root / "scripts" / "smoke" / _WRAPPER.name),
            "-Audio", str(root / "monje.wav"),
            "-AvatarImage", str(root / "monje.png"),
            "-Prompt", "The unchanged avatar speaks.",
            "-ExpectedPinnedImage", _PIN,
            "-Profiles", "reference-bench-cpu",
            "-Repetitions", "3",
            "-MaxProfileTotalSeconds", "30",
            "-OutputDir", str(output), "-NonInteractive",
        ],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "Start:ltx25_a2v_experimental", "Stop:ltx25_a2v_experimental",
    ]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8-sig"))
    assert len(manifest["runs"]) == 1
    assert manifest["skipped_profiles"] == ["reference-bench-cpu"]
