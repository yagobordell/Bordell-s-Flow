from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

MANAGER = Path("scripts/salad/manage_salad_worker.ps1")
PARALLEL = Path("scripts/smoke/prepare_parallel_benchmarks.ps1")


def _zero_replica_helper() -> str:
    script = MANAGER.read_text(encoding="utf-8")
    return "function Wait-ForStoppedZeroReplicas" + script.split(
        "function Wait-ForStoppedZeroReplicas", 1
    )[1].split("function Ensure-PreparedZeroReplicas", 1)[0]


def _run_ps(tmp_path: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    source = (
        "$ErrorActionPreference = 'Stop'\n"
        "$Service = 'ltx25'\n"
        "$GroupName = 'ai-video-factory-ltx25-worker-v2'\n"
        "function Start-Sleep { param([int]$Seconds) }\n"
        "function Get-GroupStatus { param([object]$Group) "
        "return [string]$Group.current_state.status }\n"
        "function Get-GroupDescription { param([object]$Group) return '' }\n"
        + _zero_replica_helper()
        + "\n"
        + scenario
    )
    path = tmp_path / "simulate-replicas.ps1"
    path.write_text(source, encoding="utf-8")
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(path)],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell Core is unavailable")
def test_prepare_does_not_confuse_pending_false_with_zero_replicas(tmp_path: Path) -> None:
    # The old manager returned after the FIRST GET (pending=false, replicas=1)
    # and raised prematurely. This version must keep polling until replicas=0.
    scenario = """
$script:Reads = 0
function Get-Group {
    param([hashtable]$Headers)
    $script:Reads += 1
    $Count = if ($script:Reads -eq 1) { 1 } else { 0 }
    return [pscustomobject]@{
        current_state = [pscustomobject]@{ status = 'stopped' }
        replicas = $Count
        pending_change = $false
    }
}
$Result = Wait-ForStoppedZeroReplicas -Headers @{} -TimeoutSeconds 10
if ($script:Reads -ne 4 -or [int]$Result.replicas -ne 0) {
    throw "Incorrect replica convergence: reads=$script:Reads result=$($Result.replicas)"
}
"""
    result = _run_ps(tmp_path, scenario)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell Core is unavailable")
def test_prepare_never_normalizes_a_worker_that_became_running(tmp_path: Path) -> None:
    scenario = """
function Get-Group {
    param([hashtable]$Headers)
    return [pscustomobject]@{
        current_state = [pscustomobject]@{ status = 'running' }
        replicas = 1
        pending_change = $false
    }
}
try {
    $null = Wait-ForStoppedZeroReplicas -Headers @{} -TimeoutSeconds 10
    throw 'Expected worker ownership guard'
}
catch {
    if ($_.Exception.Message -notmatch 'Refusing to normalize') { throw }
}
"""
    result = _run_ps(tmp_path, scenario)
    assert result.returncode == 0, result.stdout + result.stderr


def test_parallel_prepare_safely_recovers_a_stopped_residual_replica() -> None:
    script = PARALLEL.read_text(encoding="utf-8")
    assert script.count("foreach ($Name in $Services)") >= 3
    guard = script.index('throw "$Name must be stopped/pending=False')
    repair = script.index('& $Manager -Service $Name -Action Stop')
    prepare = script.index('$Options = @{ Service = $Name; Action = "Prepare"')
    assert guard < repair < prepare
    assert "$Name did not reach stopped/replicas=0/pending=False after cleanup." in script
    assert '"Prepare"' not in script[guard:repair]
    assert "-Recreate" not in script


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell Core is unavailable")
def test_prepare_detects_replica_rebound_during_autoscaler_poll(tmp_path: Path) -> None:
    # A one-off replicas=0 observation must not be enough: the real stopped
    # LTX group bounced 0->1 between Stop and the next Status command.
    scenario = """
$script:Reads = 0
function Get-Group {
    param([hashtable]$Headers)
    $script:Reads += 1
    $Count = if ($script:Reads -eq 3) { 1 } else { 0 }
    return [pscustomobject]@{
        current_state = [pscustomobject]@{ status = 'stopped' }
        replicas = $Count
        pending_change = $false
    }
}
$Result = Wait-ForStoppedZeroReplicas -Headers @{} -TimeoutSeconds 60
if ($script:Reads -ne 6 -or [int]$Result.replicas -ne 0) {
    throw "Missed replica rebound: reads=$script:Reads result=$($Result.replicas)"
}
"""
    result = _run_ps(tmp_path, scenario)
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_stop_restores_autoscaler_before_patching_replicas() -> None:
    script = MANAGER.read_text(encoding="utf-8")
    stop = script.split('    "Stop" {', 1)[1].split('    "Start" {', 1)[0]
    prepare = script.split('    "Prepare" {', 1)[1]

    assert "function Ensure-ManifestScaleToZero" in script
    assert '$ScaleToZeroRestore = Join-Path $PSScriptRoot' in script
    assert "-Mode Manifest -EnvFile $EnvFile -NonInteractive" in script
    assert stop.index("Ensure-ManifestScaleToZero") < stop.index(
        'Operation "normalize stopped container group to zero replicas"'
    )
    assert prepare.index("Ensure-ManifestScaleToZero") < prepare.index(
        "Ensure-PreparedZeroReplicas"
    )
    assert "AutoscalerMin" in script
    assert "queue current_queue_length=" in script
    assert "$ConsecutiveZero -ge 3" in script
