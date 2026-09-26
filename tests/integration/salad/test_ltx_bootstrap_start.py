from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MANAGER = ROOT / "scripts" / "salad" / "manage_salad_worker.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 is not installed")


def _start_function() -> str:
    source = MANAGER.read_text(encoding="utf-8")
    return source.split("function Wait-ForRunningCapacity {", maxsplit=1)[1].split(
        "function Wait-ForStoppedGroup {", maxsplit=1
    )[0].join(("function Wait-ForRunningCapacity {", ""))


def _run(fake: str, *, threshold_zero: bool = False) -> subprocess.CompletedProcess[str]:
    function = _start_function()
    if threshold_zero:
        function = function.replace("$PullStallSeconds = 480", "$PullStallSeconds = 0")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "Set-StrictMode -Version Latest\n"
        "$Service = 'ltx25'\n"
        "$GroupName = 'ltx-test'\n"
        "$ContainersBase = 'https://api.salad.com/mock'\n"
        "$script:Polls = 0\n"
        "$script:Reallocations = 0\n"
        "function Wait-ManualCapacityMutationInterval { "
        "param([double]$Seconds) "
        "$script:Polls += 1; if ($script:Polls -ge 4) { throw 'STOP_TEST' } }\n"
        "function Get-Group { param([hashtable]$Headers) "
        "return [pscustomobject]@{version=9; replicas=1; pending_change=$false;"
        "current_state=[pscustomobject]@{status='deploying'}} }\n"
        "function Get-GroupStatus { param([object]$Group) "
        "return [string]$Group.current_state.status }\n"
        f"{fake}\n"
        f"{function}\n"
        "try { $result = Wait-ForRunningCapacity -Headers @{} "
        "-ExpectedReplicas 1 -AllowBootstrappingInstance } "
        "catch { if ($_.Exception.Message -ne 'STOP_TEST') { throw } }\n"
        "Write-Output \"polls=$script:Polls reallocations=$script:Reallocations\"\n"
    )
    return subprocess.run(
        [PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_bootstrap_gate_accepts_started_instance_before_group_running() -> None:
    fake = (
        "function Invoke-SaladRequest { "
        "param([hashtable]$Headers, [string]$Uri, [string]$Operation, "
        "[string]$Method='Get', [int]$TimeoutSec=30) "
        "return [pscustomobject]@{instances=@([pscustomobject]@{"
        "id='11111111-1111-4111-8111-111111111111';version=9;"
        "state='running'; started=$true; ready=$false; pulling_progress=100})} }\n"
    )
    result = _run(fake)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "LTX instance started" in result.stdout
    assert "polls=1 reallocations=0" in result.stdout


def test_bootstrap_gate_reallocates_stalled_pull_once_with_retry_budget() -> None:
    fake = (
        "function Invoke-SaladRequest { "
        "param([hashtable]$Headers, [string]$Uri, [string]$Operation, "
        "[string]$Method='Get', [int]$TimeoutSec=30) "
        "if ($Method -eq 'Post') { "
        "if ($Uri -notmatch '/instances/.+/reallocate$') { throw 'wrong target' }; "
        "$script:Reallocations += 1; return $null }; "
        "return [pscustomobject]@{instances=@([pscustomobject]@{"
        "id='11111111-1111-4111-8111-111111111111';version=9;"
        "state='downloading';started=$false;ready=$false;pulling_progress=20})} }\n"
    )
    result = _run(fake, threshold_zero=True)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "reallocations=1" in result.stdout
    assert "polls=4" in result.stdout


def test_started_instance_is_never_reallocated_by_host_pull_watchdog() -> None:
    fake = (
        "function Invoke-SaladRequest { "
        "param([hashtable]$Headers, [string]$Uri, [string]$Operation, "
        "[string]$Method='Get', [int]$TimeoutSec=30) "
        "if ($Method -eq 'Post') { throw 'unexpected reallocation' }; "
        "return [pscustomobject]@{instances=@([pscustomobject]@{"
        "id='11111111-1111-4111-8111-111111111111';version=9;"
        "state='running';started=$true;ready=$false;pulling_progress=25})} }\n"
    )
    result = _run(fake, threshold_zero=True)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "reallocations=0" in result.stdout


def test_host_reallocation_requires_instance_progress_confirmation() -> None:
    source = _start_function()
    assert "$PullStallSeconds = 480" in source
    assert "$MaxPullReallocations = 2" in source
    assert "$RequestedReallocations.ContainsKey($Id)" in source
    assert 'Get-Group -Headers $Headers' in source
    assert "confirm stalled LTX image pull" in source
    assert "$Matching[0].started -ne $true" in source
    assert "$LastPullProgress + 0.01" in source
    assert "reallocate stalled LTX image-pull instance" in source
    assert "$InstanceState -eq \"running\"" in source
