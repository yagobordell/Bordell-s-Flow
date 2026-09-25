import shutil
import subprocess
from pathlib import Path

import pytest

WORKER = Path("scripts/salad/manage_salad_worker.ps1")
STACK = Path("scripts/salad/manage_salad_stack.ps1")


def test_stack_manager_delegates_only_compute_lifecycle() -> None:
    script = STACK.read_text(encoding="utf-8")

    assert 'ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")' in script
    assert "repair_salad_queue_attachment.ps1" not in script
    assert "start_salad_scale_to_zero.ps1" not in script
    assert "ensure_salad_zero_replicas.ps1" not in script
    assert "$Document.stack.job_transport" in script
    assert "postgres" in script
    assert "[array]::Reverse($ExecutionOrder)" in script
    assert "& $WorkerManager @Arguments" in script


def test_prepare_recreates_only_stopped_zero_replica_legacy_groups() -> None:
    script = WORKER.read_text(encoding="utf-8")

    assert "Test-LegacyQueueAttachment" in script
    assert "must be stopped at replicas=0 before Prepare" in script
    assert "Remove-StoppedContainerGroup -Headers $Headers" in script
    assert "delete legacy container group" in script
    assert "Prepared group must remain stopped." in script



def test_prepare_creates_stopped_group_with_valid_replicas_and_waits_for_visibility() -> None:
    script = WORKER.read_text(encoding="utf-8")
    create = script.split("function New-ContainerGroup {", maxsplit=1)[1].split(
        "function Update-ContainerGroup {", maxsplit=1
    )[0]
    wait = script.split("function Wait-ForGroupSettled {", maxsplit=1)[1].split(
        "function Wait-ForRunningCapacity {", maxsplit=1
    )[0]
    prepare = script.split('"Prepare" {', maxsplit=1)[1]

    assert "replicas = $StartReplicas" in create
    assert "autostart_policy = $AutostartPolicy" in create
    assert "replicas = 0" in prepare
    assert "Try-Get-Group -Headers $Headers" in wait
    assert "$VisibilityDeadline" in wait
    assert "-AllowInitialNotFound" in prepare



def test_prepare_preserves_original_api_error_when_details_are_missing() -> None:
    script = WORKER.read_text(encoding="utf-8")
    create = script.split("function New-ContainerGroup {", maxsplit=1)[1].split(
        "function Update-ContainerGroup {", maxsplit=1
    )[0]

    assert '$Details = ""' in create
    assert "if ($null -ne $_.ErrorDetails)" in create
    assert '$Details = [string]$_.ErrorDetails.Message' in create
    assert "if ((Get-HttpStatusCode -ErrorRecord $_) -ne 400" in create


def test_start_sets_explicit_replicas_before_starting_group() -> None:
    script = WORKER.read_text(encoding="utf-8")
    start = script.split('"Start" {', maxsplit=1)[1].split('"Prepare" {', maxsplit=1)[0]

    assert "set explicit replica capacity" in start
    assert '"$ContainersBase/$GroupName/start"' in start
    assert start.index("set explicit replica capacity") < start.index(
        '"$ContainersBase/$GroupName/start"'
    )
    assert "Wait-ForRunningCapacity" in start


def test_stop_converges_to_stable_zero_without_autoscaler_repair() -> None:
    script = WORKER.read_text(encoding="utf-8")
    stop = script.split('"Stop" {', maxsplit=1)[1].split('"Start" {', maxsplit=1)[0]

    assert "set replicas to zero" in stop
    assert "Wait-ForStoppedZeroReplicas" in stop
    assert "Ensure-ManifestScaleToZero" not in stop
    assert "queue_autoscaler" not in stop


def test_prepare_reads_priority_from_get_container_group_response() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is unavailable")

    probe = r"""
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Tokens = $null
$Errors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    "scripts/salad/manage_salad_worker.ps1", [ref]$Tokens, [ref]$Errors
)
if ($Errors) { throw "Worker manager has PowerShell syntax errors." }
$Assert = $Ast.Find({
    param($Node)
    $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $Node.Name -eq "Assert-PreparedGroup"
}, $true)
if ($null -eq $Assert) { throw "Assert-PreparedGroup is missing." }
. ([scriptblock]::Create($Assert.Extent.Text))
$Definition = [pscustomobject]@{ priority = "high" }
function Test-LegacyQueueAttachment { param($Group) return $false }
$Group = [pscustomobject]@{
    container = [pscustomobject]@{ image = "pinned-image" }
    priority = "high"
    replicas = 0
}
Assert-PreparedGroup -Group $Group -ResolvedImage "pinned-image"
$Group.priority = "low"
try {
    Assert-PreparedGroup -Group $Group -ResolvedImage "pinned-image"
    throw "Missing priority mismatch rejection."
}
catch {
    if ($_.Exception.Message -notmatch "expected container group priority") { throw }
}
$Group.PSObject.Properties.Remove("priority")
try {
    Assert-PreparedGroup -Group $Group -ResolvedImage "pinned-image"
    throw "Missing absent-priority rejection."
}
catch {
    if ($_.Exception.Message -notmatch "expected container group priority") { throw }
}
Write-Output "PASS: Salad container group priority contract"
"""
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", probe],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "PASS: Salad container group priority contract" in result.stdout

