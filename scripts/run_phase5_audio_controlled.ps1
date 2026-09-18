[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SourceFile,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [Parameter(Mandatory)]
    [string]$Metadata,

    [string]$Provenance = "",

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 90,

    [ValidateRange(30, 900)]
    [int]$PendingTimeoutSeconds = 300,

    [ValidateRange(60, 3600)]
    [int]$RunningTimeoutSeconds = 1200,

    [ValidateRange(1, 60)]
    [int]$PollSeconds = 5,

    [switch]$AllowUnconditionedFish,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ValidationManager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$OptimizedPrewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$QueueCleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$ZeroReplicaGuard = Join-Path $PSScriptRoot "ensure_salad_zero_replicas.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$Runner = Join-Path $PSScriptRoot "run_phase5_audio.py"
$FallbackState = Join-Path $OutputDir "fallback-state.json"

if ([string]::IsNullOrWhiteSpace($Provenance)) {
    $Provenance = Join-Path $OutputDir "narration.provenance.json"
}
if (-not (Test-Path -LiteralPath $SourceFile -PathType Leaf)) {
    throw "Phase 5 source script not found: $SourceFile"
}

function Invoke-ServiceStopAndVerify {
    param([Parameter(Mandatory)][string]$Service)

    try {
        & $ValidationManager -Action Stop -Service $Service -NonInteractive
        if (-not $?) {
            throw "Failed to stop Salad service '$Service'."
        }
    }
    finally {
        try {
            & $QueueCleanup -Service $Service -NonInteractive
            if (-not $?) {
                throw "Failed to clean Salad queue for '$Service'."
            }
        }
        finally {
            & $ZeroReplicaGuard -Service $Service -NonInteractive
            if (-not $?) {
                throw "Salad service '$Service' did not settle at replicas=0."
            }
        }
    }
}

function Invoke-Prewarm {
    param([Parameter(Mandatory)][string]$Service)

    $Arguments = @{
        Service = $Service
        TimeoutMinutes = $PrewarmTimeoutMinutes
    }
    if ($NonInteractive) {
        $Arguments["NonInteractive"] = $true
    }

    & $OptimizedPrewarm @Arguments
    if (-not $?) {
        throw "$Service optimized prewarm failed."
    }
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate any Phase 5 GPU."
}

& $ValidationManager -Action Status -Service breeze_tts2 -NonInteractive
if (-not $?) {
    throw "Breeze Salad control-plane status preflight failed."
}

$FallbackReason = $null
$BreezeSucceeded = $false

try {
    try {
        Write-Host "=== Breeze prewarm: exactly one ready primary replica ===" -ForegroundColor Cyan
        Invoke-Prewarm -Service breeze_tts2
    }
    catch {
        $FallbackReason = "breeze_prewarm_terminal_failure"
        Write-Warning (
            "Breeze prewarm failed after Salad/R2 preflight; this is eligible for Fish fallback. " +
            "Error: $($_.Exception.Message)"
        )
    }

    if ($null -eq $FallbackReason) {
        Write-Host "=== Phase 5 primary narration: Breeze ===" -ForegroundColor Cyan
        $PrimaryArguments = @(
            $SourceFile,
            "--provider", "breeze",
            "--output-dir", $OutputDir,
            "--metadata", $Metadata,
            "--provenance", $Provenance,
            "--fallback-state", $FallbackState,
            "--poll-seconds", $PollSeconds,
            "--pending-timeout-seconds", $PendingTimeoutSeconds,
            "--timeout-seconds", $RunningTimeoutSeconds
        )
        & python $Runner @PrimaryArguments
        $PrimaryExitCode = $LASTEXITCODE

        if ($PrimaryExitCode -eq 0) {
            $BreezeSucceeded = $true
        }
        elseif ($PrimaryExitCode -eq 75) {
            if (-not (Test-Path -LiteralPath $FallbackState -PathType Leaf)) {
                throw "Breeze reported eligible fallback without fallback-state.json."
            }
            $FallbackStateDocument = Get-Content -LiteralPath $FallbackState -Raw | ConvertFrom-Json
            $FallbackReason = [string]$FallbackStateDocument.fallback_reason
            if ([string]::IsNullOrWhiteSpace($FallbackReason)) {
                throw "Breeze fallback state did not contain a classification."
            }
        }
        else {
            throw (
                "Phase 5 Breeze attempt failed with non-eligible exit code $PrimaryExitCode; " +
                "Fish will not run."
            )
        }
    }
}
finally {
    Write-Host "=== Releasing Breeze before any Fish allocation ===" -ForegroundColor Cyan
    Invoke-ServiceStopAndVerify -Service breeze_tts2
}

if ($BreezeSucceeded) {
    Write-Host "Phase 5 complete with Breeze; Fish consumed zero GPU-seconds." -ForegroundColor Green
    & $ValidationManager -Action Status -Service breeze_tts2 -NonInteractive
    exit 0
}

if ([string]::IsNullOrWhiteSpace($FallbackReason)) {
    throw "Fish fallback was reached without an explicit Breeze failure classification."
}

Write-Host (
    "=== Fish fallback eligible: reason=$FallbackReason; Breeze is stopped at replicas=0 ==="
) -ForegroundColor Yellow

$FishArguments = @(
    $SourceFile,
    "--provider", "fish",
    "--output-dir", $OutputDir,
    "--metadata", $Metadata,
    "--provenance", $Provenance,
    "--fallback-state", $FallbackState,
    "--fallback-reason", $FallbackReason,
    "--poll-seconds", $PollSeconds,
    "--pending-timeout-seconds", $PendingTimeoutSeconds,
    "--timeout-seconds", $RunningTimeoutSeconds
)
if ($AllowUnconditionedFish) {
    $FishArguments += "--allow-unconditioned-fish"
}

Write-Host "=== Fish fallback configuration preflight: before GPU allocation ===" -ForegroundColor Cyan
$FishPreflightArguments = $FishArguments + "--preflight-only"
& python $Runner @FishPreflightArguments
if ($LASTEXITCODE -ne 0) {
    throw "Fish fallback configuration preflight failed; refusing Fish GPU allocation."
}

& $ValidationManager -Action Status -Service fish_speech -NonInteractive
if (-not $?) {
    throw "Fish Salad control-plane status preflight failed."
}

try {
    Write-Host "=== Fish prewarm: exactly one ready fallback replica ===" -ForegroundColor Cyan
    Invoke-Prewarm -Service fish_speech

    Write-Host "=== Phase 5 fallback narration: Fish Speech ===" -ForegroundColor Cyan
    & python $Runner @FishArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 5 Fish fallback failed with exit code $LASTEXITCODE."
    }
}
finally {
    Write-Host "=== Releasing Fish fallback GPU ===" -ForegroundColor Cyan
    Invoke-ServiceStopAndVerify -Service fish_speech
}

Write-Host "=== Final Phase 5 Salad state ===" -ForegroundColor Cyan
& $ValidationManager -Action Status -Service breeze_tts2 -NonInteractive
& $ValidationManager -Action Status -Service fish_speech -NonInteractive
Write-Host "Phase 5 complete through Fish fallback." -ForegroundColor Green
