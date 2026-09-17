[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ReferencesFile,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [Parameter(Mandatory)]
    [string]$Metadata,

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
$FluxPrewarm = Join-Path $PSScriptRoot "start_salad_flux_prewarm.ps1"
$FluxRestore = Join-Path $PSScriptRoot "restore_salad_flux_scale_to_zero.ps1"
$QueueCleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$AuditScript = Join-Path $PSScriptRoot "audit_phase4_reference_cache.py"
$Phase4Runner = Join-Path $PSScriptRoot "run_phase4_assets.py"

if (-not (Test-Path -LiteralPath $ReferencesFile -PathType Leaf)) {
    throw "Visual references file not found: $ReferencesFile"
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate image-generation GPU."
}

$PlanPath = Join-Path ([IO.Path]::GetTempPath()) (
    "ai-video-factory-phase4-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N"))
)
Write-Host "=== Phase 4 cache plan: determine which GPU, if any, is needed ===" -ForegroundColor Cyan
& python $AuditScript $ReferencesFile --json-output $PlanPath
if ($LASTEXITCODE -ne 0) {
    throw "Phase 4 cache planning failed."
}
$Plan = @(
    Get-Content -LiteralPath $PlanPath -Raw |
        ConvertFrom-Json |
        ForEach-Object { $_ }
)
Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue

$InvalidCount = @($Plan | Where-Object { $_.status -eq "invalid" }).Count
$IdeogramNeeded = @($Plan | Where-Object { $_.status -eq "miss" }).Count -gt 0
$FluxNeeded = @($Plan | Where-Object { $_.status -eq "safety_blocked" }).Count -gt 0
if ($InvalidCount -gt 0) {
    throw "Phase 4 cache contains $InvalidCount invalid artifact(s); refusing GPU allocation."
}

Write-Host (
    "Phase 4 GPU plan: ideogram={0} flux_schnell={1} cached={2}" -f `
    $IdeogramNeeded,
    $FluxNeeded,
    @($Plan | Where-Object { $_.status -eq "hit" }).Count
) -ForegroundColor Green

$PrewarmArguments = @{
    Service = "ideogram4"
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
$HoldArguments = @{ Service = "ideogram4" }
$FluxPrewarmArguments = @{ TimeoutMinutes = $PrewarmTimeoutMinutes }
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
    $HoldArguments["NonInteractive"] = $true
    $FluxPrewarmArguments["NonInteractive"] = $true
}

$IdeogramTouched = $false
$FluxTouched = $false
try {
    if ($IdeogramNeeded) {
        $IdeogramTouched = $true
        Write-Host "=== Ideogram optimized prewarm: selecting one ready node ===" -ForegroundColor Cyan
        & $OptimizedPrewarm @PrewarmArguments
        if (-not $?) {
            throw "Ideogram optimized prewarm failed."
        }
        Write-Host "=== Ideogram warm hold: pin one ready replica for the Phase 4 batch ===" `
            -ForegroundColor Cyan
        & $WarmReplicaHold @HoldArguments
        if (-not $?) {
            throw "Ideogram warm replica hold failed; refusing to submit Phase 4 jobs."
        }
    }

    if ($FluxNeeded) {
        $FluxTouched = $true
        Write-Host (
            "=== FLUX Schnell fallback: prewarm one ready replica before queue submission ==="
        ) -ForegroundColor Cyan
        & $FluxPrewarm @FluxPrewarmArguments
        if (-not $?) {
            throw "FLUX Schnell deterministic prewarm failed."
        }
    }

    if (-not $IdeogramNeeded -and -not $FluxNeeded) {
        Write-Host "All Phase 4 references are cached; no GPU allocation required." -ForegroundColor Green
    }

    Write-Host "=== Phase 4 generation: cache replay plus required queue work ===" -ForegroundColor Cyan
    & python $Phase4Runner `
        $ReferencesFile `
        --output-dir $OutputDir `
        --metadata $Metadata `
        --poll-seconds $PollSeconds `
        --pending-timeout-seconds $PendingTimeoutSeconds `
        --fallback-pending-timeout-seconds $FluxPendingTimeoutSeconds `
        --timeout-seconds $RunningTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 4 asset generation failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($IdeogramTouched) {
        try {
            & $ValidationManager -Action Stop -Service ideogram4 -NonInteractive
        }
        finally {
            & $QueueCleanup -Service ideogram4 -TimeoutSeconds 180 -NonInteractive
            & $ValidationManager -Action Status -Service ideogram4 -NonInteractive
        }
    }
    if ($FluxTouched) {
        try {
            & $FluxRestore -TimeoutSeconds 180 -NonInteractive
        }
        finally {
            & $QueueCleanup -Service flux_schnell -TimeoutSeconds 180 -NonInteractive
            & $WorkerManager -Action Status -Service flux_schnell -NonInteractive
        }
    }
}
