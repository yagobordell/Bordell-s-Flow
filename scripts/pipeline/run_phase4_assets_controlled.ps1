[CmdletBinding()]
param(
    [string]$ReferencesFile = "data/output/phase4/visual_references.json",
    [string]$OutputDir = "data/output/phase4/reference_assets",
    [string]$Metadata = "data/output/phase4/reference_assets.json",
    [ValidateRange(30, 3600)]
    [int]$PendingTimeoutSeconds = 1800,
    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,
    [ValidateRange(60, 7200)]
    [int]$RunningTimeoutSeconds = 3600,
    [ValidateRange(1, 60)]
    [int]$PollSeconds = 5,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Arguments = @{
    Phase = "Phase 4"
    RequiredInputs = @($ReferencesFile)
    CacheAuditScript = (Join-Path $PSScriptRoot "../diagnostics/audit_phase4_reference_cache.py")
    CacheAuditArguments = @($ReferencesFile)
    RunnerScript = (Join-Path $PSScriptRoot "run_phase4_assets.py")
    RunnerArguments = @(
        $ReferencesFile,
        "--poll-seconds", $PollSeconds,
        "--pending-timeout-seconds", $PendingTimeoutSeconds,
        "--timeout-seconds", $RunningTimeoutSeconds,
        "--output-dir", $OutputDir,
        "--metadata", $Metadata
    )
    PrewarmTimeoutMinutes = $PrewarmTimeoutMinutes
    NonInteractive = [bool]$NonInteractive
}

& (Join-Path $PSScriptRoot "_qwen_controlled.ps1") @Arguments
if (-not $?) {
    throw "Qwen controlled workflow failed."
}
