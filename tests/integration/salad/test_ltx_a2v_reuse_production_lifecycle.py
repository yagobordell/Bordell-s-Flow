"""Offline regression for temporary use of the stopped A2V production group."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(_PWSH is None, reason="PowerShell 7 required")
_ORIGINAL = "registry.example.com/original@sha256:" + "a" * 64
_EXPERIMENT = "registry.example.com/experiment@sha256:" + "b" * 64


@pytest.mark.parametrize("fail_ready", [False, True])
def test_reuse_restores_production_even_when_bootstrap_fails(
    tmp_path: Path, fail_ready: bool
) -> None:
    checkout = tmp_path / "checkout"
    smoke = checkout / "scripts" / "smoke"
    salad = checkout / "scripts" / "salad"
    pipeline = checkout / "scripts" / "pipeline"
    source = checkout / "src" / "ai_video_factory" / "workers" / "ltx25"
    for folder in (smoke, salad, pipeline, source, checkout / "config"):
        folder.mkdir(parents=True)
    (checkout / "config" / "app.env").write_text(
        "POSTGRES_DSN=postgresql://fixture.example.invalid/test\\n",
        encoding="utf-8",
    )
    (salad / "_env_file.ps1").write_text(
        (_ROOT / "scripts" / "salad" / "_env_file.ps1").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for name in (
        "run_ltx25_a2v_benchmark_batch.ps1",
        "run_ltx25_a2v_benchmark_reusing_production.ps1",
        "restore_ltx25_a2v_production.ps1",
    ):
        (smoke / name).write_text(
            (_ROOT / "scripts" / "smoke" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (source / "__init__.py").write_text("", encoding="utf-8")
    for name in (
        "check_ltx25_python_source.py",
        "check_ltx_a2v_benchmark_isolation.py",
        "check_ltx_a2v_benchmark_jobs.py",
    ):
        (smoke / name).write_text("print('OK')\n", encoding="utf-8")
    (pipeline / "check_r2_ready.py").write_text("print('OK')\n", encoding="utf-8")
    (smoke / "wait_salad_ltx25_ready.py").write_text(
        f"import sys\nsys.exit({1 if fail_ready else 0})\n", encoding="utf-8"
    )
    (smoke / "submit_ltx25_a2v_smoke.py").write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv\n"
        "out = pathlib.Path(args[args.index('--output-dir') + 1])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "n = int(out.name.rsplit('-', 1)[-1])\n"
        "(out / 'video.mp4').write_bytes(b'fake mp4')\n"
        "(out / 'metadata.json').write_text(json.dumps({"
        "'pipeline_reused': n > 1, 'prompt_cache_hit': n > 1, "
        "'total_elapsed_seconds': 50.0, 'cuda_peak_reserved_mb': 20000.0"
        "}))\n",
        encoding="utf-8",
    )
    (salad / "state.json").write_text(
        json.dumps({"image": _ORIGINAL, "mode": "a2v", "status": "stopped"})
    )
    (salad / "manage_salad_worker.ps1").write_text(
        """param(
    [string]$Action, [string]$Service, [string]$EnvFile, [int]$Replicas,
    [string]$PinnedImage, [string]$ExpectedCurrentImage,
    [string]$ExpectedCurrentTaskMode, [string]$TaskModeOverride,
    [switch]$SkipBuild, [switch]$PreserveExistingEnvironment,
    [switch]$AllowBootstrappingInstance, [switch]$NonInteractive
)
$ErrorActionPreference = 'Stop'
$p = Join-Path $PSScriptRoot 'state.json'
$s = Get-Content $p -Raw | ConvertFrom-Json
if ($Service -ne 'ltx25') { throw 'mutated another service' }
if ($Action -eq 'Status') {
    Write-Output "service=ltx25 status=$($s.status) image=$($s.image)"
    exit 0
}
Add-Content (Join-Path $PSScriptRoot 'events.txt') "$($Action):$($Service)"
if ($Action -eq 'Prepare') {
    if ($s.status -ne 'stopped' -or $s.image -ne $ExpectedCurrentImage -or
        $s.mode -ne $ExpectedCurrentTaskMode -or -not $PreserveExistingEnvironment) {
        throw 'guard failed'
    }
    $s.image = $PinnedImage
    $s.mode = $TaskModeOverride
} elseif ($Action -eq 'Start') {
    if ($s.mode -ne 'a2v_experimental') { throw 'wrong worker mode' }
    $s.status = 'running'
} elseif ($Action -eq 'Stop') {
    $s.status = 'stopped'
}
$s | ConvertTo-Json | Set-Content $p
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=test",
         "-c", "user.email=test@example.com",
         "commit", "--allow-empty", "-qm", "mock"],
        check=True,
    )
    audio = checkout / "audio.wav"
    avatar = checkout / "avatar.png"
    audio.write_bytes(b"fake wav")
    avatar.write_bytes(b"fake png")

    out = tmp_path / "results"
    run = subprocess.run(
        [
            _PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(smoke / "run_ltx25_a2v_benchmark_reusing_production.ps1"),
            "-Audio", str(audio), "-AvatarImage", str(avatar),
            "-Prompt", "One unchanged prompt.", "-ExperimentalImage", _EXPERIMENT,
            "-OriginalProductionImage", _ORIGINAL,
            "-Profiles", "reference-bench-cpu", "-Repetitions", "3",
            "-OutputDir", str(out),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    state = json.loads((salad / "state.json").read_text(encoding="utf-8"))
    assert state == {"image": _ORIGINAL, "mode": "a2v", "status": "stopped"}, (
        run.stdout + run.stderr
    )
    events = (salad / "events.txt").read_text(encoding="utf-8").splitlines()
    expected = ["Prepare:ltx25", "Start:ltx25", "Stop:ltx25", "Stop:ltx25", "Prepare:ltx25"]
    assert events == (expected if not fail_ready else expected), (
        run.stdout + run.stderr
    )
    assert (out / "recovery.txt").is_file()
    if fail_ready:
        assert run.returncode != 0
    else:
        assert run.returncode == 0, run.stdout + run.stderr
        assert len(json.loads((out / "manifest.json").read_text(encoding="utf-8-sig"))["runs"]) == 3
