[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,

    [ValidateRange(60, 3600)]
    [int]$RunningTimeoutSeconds = 1200,

    [ValidateRange(30, 900)]
    [int]$PendingTimeoutSeconds = 300,

    [string]$Size = "1024x1024",

    [string]$Prompt = (
        "A cinematic documentary photograph of a windswept lighthouse on a rocky Atlantic " +
        "coast at blue hour, realistic natural textures, restrained color, no text."
    ),

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Prewarm = Join-Path $PSScriptRoot "start_salad_flux_prewarm.ps1"
$Restore = Join-Path $PSScriptRoot "restore_salad_flux_scale_to_zero.ps1"
$Cleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$WorkerManager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
$Smoke = Join-Path $PSScriptRoot "run_flux2_klein_smoke.py"
$MetricCollector = Join-Path $PSScriptRoot "collect_flux2_klein_salad_metrics.ps1"

function Write-Flux2KleinSaladMetrics {
    param([Parameter(Mandatory)][datetime]$StartedAt)

    try {
        $EndedAt = (Get-Date).ToUniversalTime().AddMinutes(1)
        & $MetricCollector `
            -EnvFile $EnvFile `
            -StartTimeUtc $StartedAt.ToUniversalTime().ToString("o") `
            -EndTimeUtc $EndedAt.ToString("o") `
            -WindowMinutes 2 `
            -RetryCount 3
    }
    catch {
        Write-Warning "Salad log metric collection failed: $($_.Exception.Message)"
    }
}

$PrewarmArguments = @{
    EnvFile = $EnvFile
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
$RestoreArguments = @{
    EnvFile = $EnvFile
    TimeoutSeconds = 180
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
    $RestoreArguments["NonInteractive"] = $true
}

$PrimaryFailure = $null
$CleanupFailures = @()
$Touched = $false
$BenchmarkStartedAt = (Get-Date).ToUniversalTime().AddMinutes(-1)
Set-Location $RepoRoot

try {
    $Touched = $true
    Write-Host "=== FLUX.2 Klein prewarm from zero replicas ===" -ForegroundColor Cyan
    & $Prewarm @PrewarmArguments
    if (-not $?) {
        throw "FLUX.2 Klein prewarm failed."
    }

    Write-Host "=== FLUX.2 Klein text-to-image Salad/R2 smoke ===" -ForegroundColor Cyan
    & python $Smoke "--prompt" $Prompt "--size" $Size "--pending-timeout-seconds" $PendingTimeoutSeconds "--timeout-seconds" $RunningTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "FLUX.2 Klein smoke failed with exit code $LASTEXITCODE."
    }
}
catch {
    $PrimaryFailure = $_
}
finally {
    if ($Touched) {
        Write-Flux2KleinSaladMetrics -StartedAt $BenchmarkStartedAt
        try {
            & $Restore @RestoreArguments
        }
        catch {
            Write-Warning "FLUX.2 Klein scale-to-zero restore failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
        try {
            $CleanupArguments = @{
                Service = "flux2_klein"
                TimeoutSeconds = 180
            }
            if ($NonInteractive) {
                $CleanupArguments["NonInteractive"] = $true
            }
            & $Cleanup @CleanupArguments
        }
        catch {
            Write-Warning "FLUX.2 Klein queue cleanup failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
        try {
            $StatusArguments = @{
                Action = "Status"
                Service = "flux2_klein"
                EnvFile = $EnvFile
            }
            if ($NonInteractive) {
                $StatusArguments["NonInteractive"] = $true
            }
            & $WorkerManager @StatusArguments
        }
        catch {
            Write-Warning "FLUX.2 Klein final status verification failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
    }
}

if ($null -ne $PrimaryFailure) {
    if ($CleanupFailures.Count -gt 0) {
        Write-Warning (
            "Cleanup also reported $($CleanupFailures.Count) failure(s); " +
            "preserving the original FLUX.2 Klein smoke failure."
        )
    }
    throw $PrimaryFailure
}
if ($CleanupFailures.Count -gt 0) {
    throw $CleanupFailures[0]
}

Write-Host (
    "FLUX.2 Klein smoke complete; fallback group restored to manifest scale-to-zero."
) -ForegroundColor Green
