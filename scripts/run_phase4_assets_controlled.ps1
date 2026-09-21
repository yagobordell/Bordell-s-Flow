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

    [switch]$KeepIdeogramWarm,

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
$AuditScript = Join-Path $PSScriptRoot "audit_phase4_reference_cache.py"
$Phase4Runner = Join-Path $PSScriptRoot "run_phase4_assets.py"
$ServicesPath = Join-Path (Split-Path $PSScriptRoot -Parent) "deploy\salad\services.json"

$Services = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$IdeogramService = $Services.services.ideogram4
$FluxService = $Services.services.flux2_klein
$env:SALAD_ORGANIZATION = [string]$Services.stack.organization
$env:SALAD_PROJECT = [string]$Services.stack.project
$env:SALAD_IDEOGRAM4_QUEUE_NAME = [string]$IdeogramService.queue_name
$env:SALAD_FLUX2_KLEIN_QUEUE_NAME = [string]$FluxService.queue_name
Write-Host (
    "Phase 4 canonical Salad routes: ideogram={0} flux={1}" -f `
    $env:SALAD_IDEOGRAM4_QUEUE_NAME,
    $env:SALAD_FLUX2_KLEIN_QUEUE_NAME
) -ForegroundColor DarkGray

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
    "Phase 4 GPU plan: ideogram={0} flux2_klein={1} cached={2}" -f `
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
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
    $FluxPrewarmArguments["NonInteractive"] = $true
}

$IdeogramTouched = $false
# Fresh Ideogram work can discover a terminal safety rejection not known by the cache audit.
# In that case Python prewarms FLUX on demand before submitting the first fallback job.
$OnDemandFluxPrewarm = $IdeogramNeeded -and -not $FluxNeeded
$FluxCleanupRequired = $IdeogramNeeded -or $FluxNeeded
$PrimaryFailure = $null
$CleanupFailures = @()
try {
    if ($IdeogramNeeded) {
        $IdeogramTouched = $true
        Write-Host (
            "=== Ideogram optimized prewarm: selecting and pinning one ready node ==="
        ) -ForegroundColor Cyan
        & $OptimizedPrewarm @PrewarmArguments
        if (-not $?) {
            throw "Ideogram optimized prewarm failed."
        }
    }

    if ($FluxNeeded) {
        Write-Host (
            "=== FLUX.2 Klein fallback: prewarm one ready replica before queue submission ==="
        ) -ForegroundColor Cyan
        & $FluxPrewarm @FluxPrewarmArguments
        if (-not $?) {
            throw "FLUX.2 Klein deterministic prewarm failed."
        }
    }

    if (-not $IdeogramNeeded -and -not $FluxNeeded) {
        Write-Host "All Phase 4 references are cached; no GPU allocation required." -ForegroundColor Green
    }

    Write-Host "=== Phase 4 generation: cache replay plus required queue work ===" -ForegroundColor Cyan
    $Phase4Arguments = @(
        $ReferencesFile,
        "--output-dir", $OutputDir,
        "--metadata", $Metadata,
        "--queue-name", $env:SALAD_IDEOGRAM4_QUEUE_NAME,
        "--fallback-queue-name", $env:SALAD_FLUX2_KLEIN_QUEUE_NAME,
        "--poll-seconds", $PollSeconds,
        "--pending-timeout-seconds", $PendingTimeoutSeconds,
        "--fallback-pending-timeout-seconds", $FluxPendingTimeoutSeconds,
        "--timeout-seconds", $RunningTimeoutSeconds
    )
    if ($OnDemandFluxPrewarm) {
        $Phase4Arguments += @(
            "--prewarm-fallback-on-demand",
            "--fallback-prewarm-timeout-minutes", $PrewarmTimeoutMinutes
        )
    }
    & python $Phase4Runner @Phase4Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 4 asset generation failed with exit code $LASTEXITCODE."
    }
}
catch {
    $PrimaryFailure = $_
}
finally {
    if ($IdeogramTouched -and (-not $KeepIdeogramWarm -or $null -ne $PrimaryFailure)) {
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
    elseif ($IdeogramTouched -and $KeepIdeogramWarm) {
        Write-Host (
            "Ideogram remains warm for the next end-to-end stage; outer orchestration owns cleanup."
        ) -ForegroundColor Green
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
            "preserving the original Phase 4 failure as the terminating error."
        )
    }
    throw $PrimaryFailure
}
if ($CleanupFailures.Count -gt 0) {
    throw $CleanupFailures[0]
}
