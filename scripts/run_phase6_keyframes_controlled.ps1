[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Frames,

    [Parameter(Mandatory)]
    [string]$Shots,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [Parameter(Mandatory)]
    [string]$Output,

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,

    [ValidateRange(30, 900)]
    [int]$PendingTimeoutSeconds = 300,

    [ValidateRange(60, 3600)]
    [int]$RunningTimeoutSeconds = 1200,

    [ValidateRange(300, 3600)]
    [int]$FluxPendingTimeoutSeconds = 1800,

    [ValidateRange(1, 60)]
    [int]$PollSeconds = 5,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ValidationManager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$WorkerManager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
$OptimizedPrewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$WarmReplicaHold = Join-Path $PSScriptRoot "hold_salad_warm_replica.ps1"
$QueueCleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$Phase6Runner = Join-Path $PSScriptRoot "run_phase6_keyframes.py"

foreach ($Path in @($Frames, $Shots)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 6 input not found: $Path"
    }
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing image-generation GPU allocation."
}

$PrewarmArguments = @{ Service = "ideogram4"; TimeoutMinutes = $PrewarmTimeoutMinutes }
$HoldArguments = @{ Service = "ideogram4" }
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
    $HoldArguments["NonInteractive"] = $true
}

try {
    Write-Host "=== Ideogram optimized prewarm: selecting one ready node ===" -ForegroundColor Cyan
    & $OptimizedPrewarm @PrewarmArguments
    if (-not $?) {
        throw "Ideogram optimized prewarm failed."
    }

    Write-Host "=== Ideogram warm hold: pin one ready replica for the Phase 6 batch ===" `
        -ForegroundColor Cyan
    & $WarmReplicaHold @HoldArguments
    if (-not $?) {
        throw "Ideogram warm replica hold failed; refusing to submit Phase 6 jobs."
    }

    Write-Host "=== FLUX fallback: enable scale-to-zero group without allocating a GPU ===" `
        -ForegroundColor Cyan
    & $WorkerManager -Action Start -Service flux_schnell -NonInteractive
    if (-not $?) {
        throw "FLUX Schnell fallback group failed to start."
    }

    Write-Host "=== Phase 6 generation: Ideogram primary with FLUX safety fallback ===" `
        -ForegroundColor Cyan
    & python $Phase6Runner `
        --frames $Frames `
        --shots $Shots `
        --output-dir $OutputDir `
        --output $Output `
        --poll-seconds $PollSeconds `
        --pending-timeout-seconds $PendingTimeoutSeconds `
        --fallback-pending-timeout-seconds $FluxPendingTimeoutSeconds `
        --timeout-seconds $RunningTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 6 keyframe generation failed with exit code $LASTEXITCODE."
    }
}
finally {
    try {
        & $ValidationManager -Action Stop -Service ideogram4 -NonInteractive
    }
    finally {
        & $QueueCleanup -Service ideogram4 -TimeoutSeconds 180 -NonInteractive
        & $ValidationManager -Action Status -Service ideogram4 -NonInteractive
    }
    try {
        & $WorkerManager -Action Stop -Service flux_schnell -NonInteractive
    }
    finally {
        & $QueueCleanup -Service flux_schnell -TimeoutSeconds 180 -NonInteractive
        & $WorkerManager -Action Status -Service flux_schnell -NonInteractive
    }
}
