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
$FluxPrewarm = Join-Path $PSScriptRoot "start_salad_flux2_klein_prewarm.ps1"
$FluxRestore = Join-Path $PSScriptRoot "restore_salad_flux2_klein_scale_to_zero.ps1"
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
    throw "R2 preflight failed; refusing to allocate image-generation GPU."
}

$PrewarmArguments = @{
    Service = "ideogram4"
    TimeoutMinutes = $PrewarmTimeoutMinutes
    HoldReadyReplica = $true
}
$FluxPrewarmArguments = @{ TimeoutMinutes = $PrewarmTimeoutMinutes }
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
    $FluxPrewarmArguments["NonInteractive"] = $true
}

$IdeogramTouched = $false
$FluxTouched = $false
$PrimaryFailure = $null
$CleanupFailures = @()
try {
    $IdeogramTouched = $true
    Write-Host (
        "=== Ideogram optimized prewarm: selecting and pinning one ready node ==="
    ) -ForegroundColor Cyan
    & $OptimizedPrewarm @PrewarmArguments
    if (-not $?) {
        throw "Ideogram optimized prewarm failed."
    }

    $FluxTouched = $true
    Write-Host "=== FLUX fallback: prewarm one ready replica for deterministic safety fallback ===" `
        -ForegroundColor Cyan
    & $FluxPrewarm @FluxPrewarmArguments
    if (-not $?) {
        throw "FLUX.2 Klein deterministic prewarm failed."
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
    if ($FluxTouched) {
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
