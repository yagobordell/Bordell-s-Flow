[CmdletBinding()]
param(
    [string]$EnvFile = ".env",
    [string]$SessionId = "",
    [switch]$AllowUnconditionedFish,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$WorkerManager = Join-Path $RepoRoot "scripts\salad\manage_salad_worker.ps1"
$R2Preflight = Join-Path $RepoRoot "scripts\pipeline\check_r2_ready.py"
$Smoke = Join-Path $PSScriptRoot "run_fish_speech_smoke.py"
$RoutingSmoke = Join-Path $PSScriptRoot "run_fish_speech_fallback_smoke.py"
$Metrics = Join-Path $RepoRoot "scripts\diagnostics\collect_fish_speech_salad_metrics.ps1"
$OutputDir = Join-Path $RepoRoot "data\output\fish-speech-smoke"

if ([string]::IsNullOrWhiteSpace($SessionId)) {
    $SessionId = [Guid]::NewGuid().ToString("N")
}

function Invoke-FishStop {
    & $WorkerManager -Action Stop -Service fish_speech -EnvFile $EnvFile -NonInteractive
    if (-not $?) { throw "Failed to stop Fish Salad compute group." }
}

$SmokeArguments = @(
    "--session-id", $SessionId,
    "--output-dir", $OutputDir
)
if ($AllowUnconditionedFish) {
    $SmokeArguments += "--allow-unconditioned"
}

Write-Host "=== Fish smoke R2 preflight ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing Fish GPU allocation."
}

Write-Host "=== Fish configuration preflight: before GPU allocation ===" -ForegroundColor Cyan
& python $Smoke @SmokeArguments --preflight-only
if ($LASTEXITCODE -ne 0) {
    throw "Fish smoke configuration preflight failed; refusing Fish GPU allocation."
}

$StartArguments = @{
    Action = "Start"
    Service = "fish_speech"
    Replicas = 1
    EnvFile = $EnvFile
}
if ($NonInteractive) {
    $StartArguments["NonInteractive"] = $true
}

try {
    Write-Host "=== Fish explicit capacity: one RTX 4090 replica ===" -ForegroundColor Cyan
    & $WorkerManager @StartArguments
    if (-not $?) { throw "Fish compute-group start failed." }

    Write-Host "=== Fish real inference + R2 roundtrip smoke ===" -ForegroundColor Cyan
    & python $Smoke @SmokeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Fish real smoke failed with exit code $LASTEXITCODE."
    }

    Write-Host "=== Fake Breeze eligible failure -> real Fish routing smoke ===" -ForegroundColor Cyan
    & python $RoutingSmoke
    if ($LASTEXITCODE -ne 0) {
        throw "Fish fallback routing smoke failed with exit code $LASTEXITCODE."
    }
}
finally {
    Write-Host "=== Fish smoke cleanup: stable replicas=0 ===" -ForegroundColor Cyan
    Invoke-FishStop
}

Write-Host "=== Fish replay with service stopped ===" -ForegroundColor Cyan
& python $Smoke @SmokeArguments --expect-replay
if ($LASTEXITCODE -ne 0) {
    throw "Fish replay smoke failed; replay must succeed without a GPU."
}

& $WorkerManager -Action Status -Service fish_speech -EnvFile $EnvFile -NonInteractive

Write-Host "=== Fish runtime/inference metrics from Salad logs ===" -ForegroundColor Cyan
& $Metrics -EnvFile $EnvFile
if (-not $?) {
    throw "Fish Salad metric collection failed."
}

Write-Host (
    "Fish real smoke complete: inference, routing, replay, stable-zero cleanup. Session=$SessionId"
) -ForegroundColor Green
