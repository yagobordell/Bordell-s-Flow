[CmdletBinding()]
param(
    [string]$Frames = "data/output/phase6/storyboard_frames.json",
    [string]$Shots = "data/output/phase3/shots.json",
    [string]$OutputDir = "data/output/phase6/storyboard_keyframes",
    [string]$Output = "data/output/phase6/storyboard_keyframes.json",
    [int]$PendingTimeoutSeconds = 1800,
    [int]$PrewarmTimeoutMinutes = 60,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Manifest = Get-Content (Join-Path $RepoRoot "deploy\salad\services.json") -Raw | ConvertFrom-Json
$Service = $Manifest.services.qwen_image_21
$env:SALAD_QWEN_IMAGE_21_QUEUE_NAME = [string]$Service.queue_name

$Prewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$Restore = Join-Path $PSScriptRoot "restore_salad_scale_to_zero.ps1"
$Cleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"

$prewarmArgs = @{
    Service = "qwen_image_21"
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
if ($NonInteractive) { $prewarmArgs["NonInteractive"] = $true }

try {
    Write-Host "=== Phase 6 Qwen-Image-2.1 prewarm ===" -ForegroundColor Cyan
    & $Prewarm @prewarmArgs
    if (-not $?) { throw "Qwen-Image-2.1 prewarm failed." }

    $args = @(
        (Join-Path $PSScriptRoot "run_phase6_keyframes.py"),
        "--frames", $Frames,
        "--shots", $Shots,
        "--queue-name", $env:SALAD_QWEN_IMAGE_21_QUEUE_NAME,
        "--pending-timeout-seconds", $PendingTimeoutSeconds,
        "--output-dir", $OutputDir,
        "--output", $Output
    )
    python @args
    if ($LASTEXITCODE -ne 0) { throw "Phase 6 Qwen keyframe generation failed." }
}
finally {
    try { & $Restore -Service qwen_image_21 -TimeoutSeconds 180 -NonInteractive } catch { Write-Warning $_ }
    try { & $Cleanup -Service qwen_image_21 -TimeoutSeconds 180 -NonInteractive } catch { Write-Warning $_ }
}
