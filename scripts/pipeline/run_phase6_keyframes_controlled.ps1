[CmdletBinding()]
param(
    [string]$Frames = "data/output/phase6/storyboard_frames.json",
    [string]$Shots = "data/output/phase3/shots.json",
    [string]$OutputDir = "data/output/phase6/storyboard_keyframes",
    [string]$Output = "data/output/phase6/storyboard_keyframes.json",
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
    Phase = "Phase 6"
    RequiredInputs = @($Frames, $Shots)
    CacheAuditScript = (Join-Path $PSScriptRoot "../diagnostics/audit_phase6_keyframe_cache.py")
    CacheAuditArguments = @($Frames)
    RunnerScript = (Join-Path $PSScriptRoot "run_phase6_keyframes.py")
    RunnerArguments = @(
        "--frames", $Frames,
        "--shots", $Shots,
        "--poll-seconds", $PollSeconds,
        "--pending-timeout-seconds", $PendingTimeoutSeconds,
        "--timeout-seconds", $RunningTimeoutSeconds,
        "--output-dir", $OutputDir,
        "--output", $Output
    )
    PrewarmTimeoutMinutes = $PrewarmTimeoutMinutes
    NonInteractive = [bool]$NonInteractive
}

& (Join-Path $PSScriptRoot "_qwen_controlled.ps1") @Arguments
if (-not $?) {
    throw "Qwen controlled workflow failed."
}
