[CmdletBinding()]
param(
    [string]$ReferencesFile = "data/output/phase4/visual_references.json",
    [string]$OutputDir = "data/output/phase4/reference_assets",
    [string]$Metadata = "data/output/phase4/reference_assets.json",
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
    Write-Host "=== Phase 4 Qwen-Image-2.1 prewarm ===" -ForegroundColor Cyan
    & $Prewarm @prewarmArgs
    if (-not $?) { throw "Qwen-Image-2.1 prewarm failed." }

    $args = @(
        (Join-Path $PSScriptRoot "run_phase4_assets.py"),
        $ReferencesFile,
        "--queue-name", $env:SALAD_QWEN_IMAGE_21_QUEUE_NAME,
        "--pending-timeout-seconds", $PendingTimeoutSeconds,
        "--output-dir", $OutputDir,
        "--metadata", $Metadata
    )
    python @args
    if ($LASTEXITCODE -ne 0) { throw "Phase 4 Qwen image generation failed." }
}
finally {
    try { & $Restore -Service qwen_image_21 -TimeoutSeconds 180 -NonInteractive } catch { Write-Warning $_ }
    try { & $Cleanup -Service qwen_image_21 -TimeoutSeconds 180 -NonInteractive } catch { Write-Warning $_ }
}
