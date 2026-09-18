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
$FluxPrewarm = Join-Path $PSScriptRoot "start_salad_flux_prewarm.ps1"
$FluxRestore = Join-Path $PSScriptRoot "restore_salad_flux_scale_to_zero.ps1"
$QueueCleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$AuditScript = Join-Path $PSScriptRoot "audit_phase6_keyframe_cache.py"
$Phase6Runner = Join-Path $PSScriptRoot "run_phase6_keyframes.py"

foreach ($Path in @($Frames, $Shots)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 6 input not found: $Path"
    }
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate image-generation GPU."
}

$PlanPath = Join-Path ([IO.Path]::GetTempPath()) (
    "ai-video-factory-phase6-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N"))
)
Write-Host "=== Phase 6 cache plan: determine required GPU work before prewarm ===" -ForegroundColor Cyan
& python $AuditScript $Frames --json-output $PlanPath
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
    throw "Phase 6 cache planning failed; refusing GPU allocation."
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
    throw "Phase 6 cache contains $InvalidCount invalid artifact(s); refusing GPU allocation."
}
Write-Host (
    "Phase 6 GPU plan: ideogram={0} flux2_klein={1} cached={2}" -f `
    $IdeogramNeeded,
    $FluxNeeded,
    @($Plan | Where-Object { $_.status -eq "hit" }).Count
) -ForegroundColor Green

$PrewarmArguments = @{
    Service = "ideogram4"
    TimeoutMinutes = $PrewarmTimeoutMinutes
    HoldReadyReplica = $true
}
$FluxPrewarmArguments = @{ TimeoutMinutes = $PrewarmTimeoutMinutes }
$FluxArmArguments = @{
    Action = "Start"
    Service = "flux2_klein"
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
    $FluxPrewarmArguments["NonInteractive"] = $true
    $FluxArmArguments["NonInteractive"] = $true
}

$IdeogramTouched = $false
# Fresh Ideogram work can discover a safety rejection not yet represented in R2.
# Arm FLUX at scale-to-zero for that case, but do not allocate its GPU preemptively.
$FluxCleanupRequired = $IdeogramNeeded -or $FluxNeeded
$PrimaryFailure = $null
$CleanupFailures = @()
try {
    if ($IdeogramNeeded -and -not $FluxNeeded) {
        Write-Host (
            "=== FLUX fallback: arm scale-to-zero group without allocating a GPU ==="
        ) -ForegroundColor Cyan
        $FluxArmSucceeded = $false
        for ($Attempt = 1; $Attempt -le 3; $Attempt += 1) {
            try {
                & $WorkerManager @FluxArmArguments
                if (-not $?) {
                    throw "FLUX.2 Klein scale-to-zero group start failed."
                }
                $FluxArmSucceeded = $true
                break
            }
            catch {
                if ($Attempt -ge 3) {
                    throw
                }
                Write-Warning (
                    "FLUX arm attempt $Attempt failed while Salad may still be applying the remote start: " +
                    "$($_.Exception.Message) Retrying idempotently."
                )
                Start-Sleep -Seconds 15
            }
        }
        if (-not $FluxArmSucceeded) {
            throw "FLUX.2 Klein scale-to-zero group could not be armed for dynamic fallback."
        }
    }

    if ($IdeogramNeeded) {
        $IdeogramTouched = $true
        Write-Host (
            "=== Ideogram optimized prewarm: select one ready node only when cache misses exist ==="
        ) -ForegroundColor Cyan
        & $OptimizedPrewarm @PrewarmArguments
        if (-not $?) {
            throw "Ideogram optimized prewarm failed."
        }
    }

    if ($FluxNeeded) {
        Write-Host (
            "=== FLUX fallback: prewarm only because cached safety evidence requires it ==="
        ) -ForegroundColor Cyan
        & $FluxPrewarm @FluxPrewarmArguments
        if (-not $?) {
            throw "FLUX.2 Klein deterministic prewarm failed."
        }
    }

    if (-not $IdeogramNeeded -and -not $FluxNeeded) {
        Write-Host "All Phase 6 keyframes are cached; no GPU allocation required." `
            -ForegroundColor Green
    }

    Write-Host "=== Phase 6 generation: replay plus required Ideogram/FLUX work ===" `
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
catch {
    $PrimaryFailure = $_
}
finally {
    if ($IdeogramTouched) {
        try {
            & $ValidationManager -Action Stop -Service ideogram4 -NonInteractive
        }
        catch {
            Write-Warning "Ideogram stop failed during cleanup: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
        try {
            & $QueueCleanup -Service ideogram4 -TimeoutSeconds 180 -NonInteractive
        }
        catch {
            Write-Warning "Ideogram queue cleanup failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
        try {
            & $ValidationManager -Action Status -Service ideogram4 -NonInteractive
        }
        catch {
            Write-Warning "Ideogram status verification failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
    }
    if ($FluxCleanupRequired) {
        try {
            & $FluxRestore -TimeoutSeconds 180 -NonInteractive
        }
        catch {
            Write-Warning "FLUX restore failed during cleanup: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
        try {
            & $QueueCleanup -Service flux2_klein -TimeoutSeconds 180 -NonInteractive
        }
        catch {
            Write-Warning "FLUX queue cleanup failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
        try {
            & $WorkerManager -Action Status -Service flux2_klein -NonInteractive
        }
        catch {
            Write-Warning "FLUX status verification failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
    }
}

if ($null -ne $PrimaryFailure) {
    if ($CleanupFailures.Count -gt 0) {
        Write-Warning (
            "Cleanup also reported $($CleanupFailures.Count) failure(s); " +
            "preserving the original Phase 6 failure as the terminating error."
        )
    }
    throw $PrimaryFailure
}
if ($CleanupFailures.Count -gt 0) {
    throw $CleanupFailures[0]
}
