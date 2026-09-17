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
$Audit = Join-Path $PSScriptRoot "audit_phase6_keyframe_cache.py"
$Phase6Runner = Join-Path $PSScriptRoot "run_phase6_keyframes.py"

foreach ($Path in @($Frames, $Shots)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 6 input not found: $Path"
    }
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate image GPU."
}

$PlanPath = Join-Path ([IO.Path]::GetTempPath()) (
    "phase6-image-plan-{0}.json" -f [Guid]::NewGuid().ToString("N")
)
& python $Audit --frames $Frames --json-output $PlanPath
if ($LASTEXITCODE -ne 0) {
    throw "Phase 6 cache/safety preflight failed."
}
$Plan = @(Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json)
$NeedsIdeogram = @($Plan | Where-Object { $_.primary_status -eq "miss" }).Count -gt 0
$NeedsFlux = @($Plan | Where-Object { $_.status -eq "safety_blocked" }).Count -gt 0
$NeedsWork = @($Plan | Where-Object { $_.status -ne "hit" }).Count -gt 0

$PrewarmArguments = @{Service = "ideogram4"; TimeoutMinutes = $PrewarmTimeoutMinutes}
$HoldArguments = @{Service = "ideogram4"}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
    $HoldArguments["NonInteractive"] = $true
}

try {
    if ($NeedsIdeogram) {
        Write-Host "=== Ideogram optimized prewarm: primary keyframe miss exists ===" -ForegroundColor Cyan
        & $OptimizedPrewarm @PrewarmArguments
        if (-not $?) {
            throw "Ideogram optimized prewarm failed."
        }
        Write-Host "=== Ideogram warm hold: pin one ready primary replica ===" -ForegroundColor Cyan
        & $WarmReplicaHold @HoldArguments
        if (-not $?) {
            throw "Ideogram warm replica hold failed."
        }
    }
    elseif ($NeedsFlux) {
        Write-Host (
            "Ideogram has no executable misses; known safety blocks will cold-start FLUX only."
        ) -ForegroundColor Cyan
    }
    else {
        Write-Host "Phase 6 keyframe plan is fully cached; no image GPU prewarm required." -ForegroundColor Green
    }

    Write-Host "=== Phase 6 generation: cache-aware Ideogram + FLUX fallback ===" -ForegroundColor Cyan
    & python $Phase6Runner `
        --frames $Frames `
        --shots $Shots `
        --output-dir $OutputDir `
        --output $Output `
        --poll-seconds $PollSeconds `
        --pending-timeout-seconds $PendingTimeoutSeconds `
        --flux-pending-timeout-seconds $FluxPendingTimeoutSeconds `
        --timeout-seconds $RunningTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 6 keyframe generation failed with exit code $LASTEXITCODE."
    }
}
finally {
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
    try {
        & $ValidationManager -Action Stop -Service ideogram4 -NonInteractive
    }
    finally {
        try {
            & $QueueCleanup -Service ideogram4 -TimeoutSeconds 180 -NonInteractive
        }
        finally {
            if ($NeedsWork) {
                try {
                    & $WorkerManager -Action Stop -Service flux_schnell -NonInteractive
                }
                finally {
                    & $QueueCleanup -Service flux_schnell -TimeoutSeconds 180 -NonInteractive
                }
            }
            & $WorkerManager -Action Status -Service ideogram4 -NonInteractive
            if ($NeedsWork) {
                & $WorkerManager -Action Status -Service flux_schnell -NonInteractive
            }
        }
    }
}
