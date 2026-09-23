[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 90,

    [switch]$AllowUnconditionedFish,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Prewarm = Join-Path $PSScriptRoot "../salad/start_salad_optimized_prewarm.ps1"
$Validation = Join-Path $PSScriptRoot "../salad/manage_salad_validation.ps1"
$Cleanup = Join-Path $PSScriptRoot "../salad/cleanup_salad_queue.ps1"
$ZeroGuard = Join-Path $PSScriptRoot "../salad/ensure_salad_zero_replicas.ps1"
$R2Preflight = Join-Path $PSScriptRoot "../pipeline/check_r2_ready.py"
$Smoke = Join-Path $PSScriptRoot "run_fish_speech_smoke.py"
$RoutingSmoke = Join-Path $PSScriptRoot "run_fish_speech_fallback_smoke.py"
$Metrics = Join-Path $PSScriptRoot "../diagnostics/collect_fish_speech_salad_metrics.ps1"
$OutputDir = Join-Path $RepoRoot "data\output\fish-speech-smoke"
$SessionId = [guid]::NewGuid().ToString("N")

function Stop-FishAndVerify {
    try {
        & $Validation -Action Stop -Service fish_speech -EnvFile $EnvFile -NonInteractive
        if (-not $?) {
            throw "Failed to stop Fish Salad service."
        }
    }
    finally {
        try {
            & $Cleanup -Service fish_speech -EnvFile $EnvFile -NonInteractive
            if (-not $?) {
                throw "Failed to clean Fish Salad queue."
            }
        }
        finally {
            & $ZeroGuard -Service fish_speech -EnvFile $EnvFile -NonInteractive
            if (-not $?) {
                throw "Fish Salad service did not settle at replicas=0."
            }
        }
    }
}

Write-Host "=== Fish smoke R2 preflight ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing Fish GPU allocation."
}

& $Validation -Action Status -Service fish_speech -EnvFile $EnvFile -NonInteractive
if (-not $?) {
    throw "Fish Salad service status preflight failed."
}

$PrewarmArguments = @{
    Service = "fish_speech"
    EnvFile = $EnvFile
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

$SmokeArguments = @(
    "--session-id", $SessionId,
    "--output-dir", $OutputDir
)
$RoutingArguments = @(
    "--session-id", $SessionId,
    "--output-dir", $OutputDir
)
if ($AllowUnconditionedFish) {
    $SmokeArguments += "--allow-unconditioned"
    $RoutingArguments += "--allow-unconditioned"
}

Write-Host "=== Fish configuration preflight: before GPU allocation ===" -ForegroundColor Cyan
$PreflightArguments = $SmokeArguments + "--preflight-only"
& python $Smoke @PreflightArguments
if ($LASTEXITCODE -ne 0) {
    throw "Fish smoke configuration preflight failed; refusing Fish GPU allocation."
}

try {
    Write-Host "=== Fish real prewarm: one RTX 4090 replica maximum ===" -ForegroundColor Cyan
    & $Prewarm @PrewarmArguments
    if (-not $?) {
        throw "Fish optimized prewarm failed."
    }

    Write-Host "=== Fish real inference + R2 roundtrip smoke ===" -ForegroundColor Cyan
    & python $Smoke @SmokeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Fish real smoke failed with exit code $LASTEXITCODE."
    }

    Write-Host "=== Fake Breeze eligible failure -> real Fish routing smoke ===" -ForegroundColor Cyan
    & python $RoutingSmoke @RoutingArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Fish fallback routing smoke failed with exit code $LASTEXITCODE."
    }
}
finally {
    Write-Host "=== Fish smoke cleanup before replay ===" -ForegroundColor Cyan
    Stop-FishAndVerify
}

Write-Host "=== Fish replay with service stopped at replicas=0 ===" -ForegroundColor Cyan
$ReplayArguments = $SmokeArguments + "--expect-replay"
& python $Smoke @ReplayArguments
if ($LASTEXITCODE -ne 0) {
    throw "Fish replay smoke failed; cache replay must succeed without a GPU."
}

Write-Host "=== Fish final scale-to-zero and queue state ===" -ForegroundColor Cyan
Stop-FishAndVerify
& $Validation -Action Status -Service fish_speech -EnvFile $EnvFile -NonInteractive

Write-Host "=== Fish runtime/inference metrics from Salad logs ===" -ForegroundColor Cyan
& $Metrics -EnvFile $EnvFile -SinceMinutes 120
if (-not $?) {
    throw "Fish Salad metric collection failed."
}

Write-Host (
    "Fish real smoke complete: inference, routing, replay, cleanup. Session=$SessionId"
) -ForegroundColor Green
