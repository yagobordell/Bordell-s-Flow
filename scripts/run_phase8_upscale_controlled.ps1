[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Clips,
    [Parameter(Mandatory)][string]$OutputDir,
    [string]$EnvFile = ".env",
    [ValidateRange(10, 180)][int]$PrewarmTimeoutMinutes = 45,
    [ValidateRange(60, 14400)][int]$TimeoutSeconds = 7200,
    [ValidateRange(30, 1800)][int]$DispatchTimeoutSeconds = 300,
    [ValidateRange(0, 2)][int]$DispatchRecoveryAttempts = 1,
    [ValidateRange(1, 60)][int]$PollSeconds = 10,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path (Split-Path $PSScriptRoot -Parent) $Resolved
    }
    if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
        throw "Video Factory environment file not found: $Resolved"
    }

    foreach ($RawLine in Get-Content -LiteralPath $Resolved) {
        $Line = $RawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($Line) -or $Line.StartsWith("#")) {
            continue
        }
        if ($Line -notmatch '^(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$') {
            continue
        }
        $Name = $Matches["name"]
        $Value = $Matches["value"].Trim()
        if ($Value.Length -ge 2) {
            $First = $Value.Substring(0, 1)
            $Last = $Value.Substring($Value.Length - 1, 1)
            if (($First -eq '"' -and $Last -eq '"') -or ($First -eq "'" -and $Last -eq "'")) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }
        }
        [Environment]::SetEnvironmentVariable(
            $Name,
            $Value,
            [EnvironmentVariableTarget]::Process
        )
    }
}

Import-EnvFile -Path $EnvFile

$ValidationManager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$ScaleToZeroStarter = Join-Path $PSScriptRoot "start_salad_scale_to_zero.ps1"
$OptimizedPrewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$QueueGuard = Join-Path $PSScriptRoot "check_salad_queue_ready.py"
$QueueCleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$CacheAudit = Join-Path $PSScriptRoot "audit_phase8_upscale_cache.py"
$ManifestInspector = Join-Path $PSScriptRoot "inspect_phase8_upscale_manifest.py"
$FailureInspector = Join-Path $PSScriptRoot "inspect_inference_job_error.py"
$Runner = Join-Path $PSScriptRoot "run_phase8_upscale.py"
$ServicesPath = Join-Path (Split-Path $PSScriptRoot -Parent) "deploy\salad\services.json"

function Invoke-DispatchRecovery {
    param([Parameter(Mandatory)][int]$Attempt)

    Write-Warning (
        "Real-ESRGAN transport made no observable dispatch; recycling the worker " +
        "before bounded recovery attempt {0}/{1}." -f $Attempt, $DispatchRecoveryAttempts
    )
    & $ValidationManager -Action Stop -Service realesrgan -EnvFile $EnvFile -NonInteractive
    if ($LASTEXITCODE -ne 0) {
        throw "Real-ESRGAN worker stop failed during dispatch recovery."
    }
    & $QueueCleanup -Service realesrgan -EnvFile $EnvFile -TimeoutSeconds 180 -NonInteractive
    if ($LASTEXITCODE -ne 0) {
        throw "Real-ESRGAN queue cleanup failed during dispatch recovery."
    }

    $RecoveryPrewarmArguments = @{
        Service = "realesrgan"
        EnvFile = $EnvFile
        TimeoutMinutes = $PrewarmTimeoutMinutes
        HoldReadyReplica = $true
    }
    if ($NonInteractive) { $RecoveryPrewarmArguments["NonInteractive"] = $true }
    & $OptimizedPrewarm @RecoveryPrewarmArguments
    if (-not $?) {
        throw "Real-ESRGAN optimized prewarm failed during dispatch recovery."
    }
}

if (-not (Test-Path -LiteralPath $Clips -PathType Leaf)) {
    throw "Required Phase 8 clips metadata not found: $Clips"
}

$Services = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$UpscaleService = $Services.services.realesrgan
$env:SALAD_ORGANIZATION = [string]$Services.stack.organization
$env:SALAD_PROJECT = [string]$Services.stack.project
$env:SALAD_REALESRGAN_QUEUE_NAME = [string]$UpscaleService.queue_name
Write-Host ("Phase 8 upscale Salad route: queue={0}" -f $env:SALAD_REALESRGAN_QUEUE_NAME) -ForegroundColor DarkGray

$ManifestPath = Join-Path $OutputDir "video_upscale_manifest.json"
$ProductionOutputRoot = Split-Path -Parent $OutputDir
if ([string]::IsNullOrWhiteSpace($ProductionOutputRoot)) { $ProductionOutputRoot = "." }

$ManifestStatePath = Join-Path ([IO.Path]::GetTempPath()) ("ai-video-factory-upscale-manifest-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
& python $ManifestInspector --clips $Clips --manifest $ManifestPath --json-output $ManifestStatePath
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $ManifestStatePath -Force -ErrorAction SilentlyContinue
    throw "Real-ESRGAN resume manifest inspection failed; refusing GPU allocation."
}
$ManifestState = Get-Content -LiteralPath $ManifestStatePath -Raw | ConvertFrom-Json
Remove-Item -LiteralPath $ManifestStatePath -Force -ErrorAction SilentlyContinue
$ResumeSubmittedJobs = ([string]$ManifestState.status -eq "matching" -and [int]$ManifestState.active_resume_jobs -gt 0)
$RetryTerminalOnly = (
    [string]$ManifestState.status -eq "matching" -and
    [int]$ManifestState.active_resume_jobs -eq 0 -and
    [int]$ManifestState.terminal_retry_jobs -gt 0
)

if ([string]$ManifestState.status -eq "different_plan") {
    Write-Host "=== Real-ESRGAN stale manifest: prove queue idle before archival ===" -ForegroundColor Cyan
    & python $QueueGuard realesrgan --output-dir $ProductionOutputRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Different upscale plan found while Real-ESRGAN queue is not provably idle."
    }
    $ArchiveStatePath = Join-Path ([IO.Path]::GetTempPath()) ("ai-video-factory-upscale-archive-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
    & python $ManifestInspector --clips $Clips --manifest $ManifestPath --json-output $ArchiveStatePath --archive-mismatch
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $ArchiveStatePath -Force -ErrorAction SilentlyContinue
        throw "Real-ESRGAN stale manifest archival failed."
    }
    Remove-Item -LiteralPath $ArchiveStatePath -Force -ErrorAction SilentlyContinue
    $ResumeSubmittedJobs = $false
    $RetryTerminalOnly = $false
}

Write-Host "=== R2 preflight before Real-ESRGAN allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; refusing Real-ESRGAN GPU allocation." }

$CachePlanPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-video-factory-upscale-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
Write-Host "=== Real-ESRGAN cache plan before GPU allocation ===" -ForegroundColor Cyan
& python $CacheAudit --clips $Clips --json-output $CachePlanPath
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
    throw "Real-ESRGAN cache audit failed; refusing GPU allocation."
}
$CachePlan = @(Get-Content -LiteralPath $CachePlanPath -Raw | ConvertFrom-Json)
Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
$InvalidCache = @($CachePlan | Where-Object { $_.status -eq "invalid" }).Count
if ($InvalidCache -gt 0) { throw "Real-ESRGAN cache contains $InvalidCache invalid artifact(s)." }
$UpscaleNeeded = @($CachePlan | Where-Object { $_.status -eq "miss" }).Count -gt 0
Write-Host ("Real-ESRGAN GPU plan: needed={0} cached={1} misses={2}" -f $UpscaleNeeded, @($CachePlan | Where-Object { $_.status -eq "hit" }).Count, @($CachePlan | Where-Object { $_.status -eq "miss" }).Count) -ForegroundColor Green

try {
    if (-not $UpscaleNeeded) {
        Write-Host "All 1440p clips are valid R2 replays; Real-ESRGAN GPU allocation is skipped." -ForegroundColor Green
    }
    elseif ($ResumeSubmittedJobs) {
        Write-Host "=== Real-ESRGAN resume: verify queue ownership ===" -ForegroundColor Cyan
        & python $QueueGuard realesrgan --output-dir $ProductionOutputRoot
        if ($LASTEXITCODE -ne 0) { throw "Real-ESRGAN resume queue ownership guard failed." }
        $StartArguments = @{ Service = "realesrgan" }
        if ($NonInteractive) { $StartArguments["NonInteractive"] = $true }
        & $ScaleToZeroStarter @StartArguments
        if (-not $?) { throw "Real-ESRGAN scale-to-zero resume start failed." }
    }
    else {
        $PrewarmArguments = @{ Service = "realesrgan"; TimeoutMinutes = $PrewarmTimeoutMinutes }
        if ($RetryTerminalOnly) {
            $PrewarmArguments["HoldReadyReplica"] = $true
            Write-Host (
                "=== Real-ESRGAN terminal retry: prewarm exactly one fresh RTX 3090 ==="
            ) -ForegroundColor Cyan
            Write-Host (
                "Retrying only terminal upscale transports at min_replicas=1/max_replicas=1; " +
                "validated 1440p R2 outputs remain cached."
            ) -ForegroundColor Yellow
        }
        else {
            Write-Host "=== Real-ESRGAN optimized prewarm ===" -ForegroundColor Cyan
        }
        if ($NonInteractive) { $PrewarmArguments["NonInteractive"] = $true }
        & $OptimizedPrewarm @PrewarmArguments
        if (-not $?) { throw "Real-ESRGAN optimized prewarm failed." }
    }

    $RecoveryAttempt = 0
    while ($true) {
        & python $Runner `
            --clips $Clips `
            --output-dir $OutputDir `
            --poll-seconds $PollSeconds `
            --timeout-seconds $TimeoutSeconds `
            --dispatch-timeout-seconds $DispatchTimeoutSeconds
        if ($LASTEXITCODE -eq 0) {
            break
        }

        $UpscaleExitCode = $LASTEXITCODE
        $CanRecoverDispatch = $false
        if (Test-Path -LiteralPath $ManifestPath -PathType Leaf) {
            try {
                $FailureManifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
                $ActiveTransports = @(
                    $FailureManifest.jobs |
                        Where-Object { [string]$_.transport_status -in @("pending", "running") }
                ).Count
                $FailedTransports = @(
                    $FailureManifest.jobs |
                        Where-Object { [string]$_.transport_status -eq "failed" }
                ).Count
                $CancelledTransports = @(
                    $FailureManifest.jobs |
                        Where-Object { [string]$_.transport_status -eq "cancelled" }
                ).Count
                $CanRecoverDispatch = (
                    $ActiveTransports -eq 0 -and
                    $FailedTransports -eq 0 -and
                    $CancelledTransports -gt 0
                )
                $TerminalFailures = @(
                    $FailureManifest.jobs |
                        Where-Object { [string]$_.transport_status -in @("failed", "cancelled") }
                )
                foreach ($Failure in $TerminalFailures) {
                    Write-Host (
                        "=== Real-ESRGAN persisted failure: shot={0} transport={1} ===" -f `
                        [int]$Failure.shot_id,
                        [string]$Failure.transport_job_id
                    ) -ForegroundColor Yellow
                    & python $FailureInspector `
                        --application-job-id ([string]$Failure.application_job_id)
                    if ($LASTEXITCODE -ne 0) {
                        Write-Warning (
                            "Could not read persisted gpu.jobs diagnostics for upscale shot " +
                            ([string]$Failure.shot_id) + "."
                        )
                    }
                }
            }
            catch {
                Write-Warning (
                    "Could not inspect persisted Real-ESRGAN terminal failures: " +
                    $_.Exception.Message
                )
            }
        }
        if ($CanRecoverDispatch -and $RecoveryAttempt -lt $DispatchRecoveryAttempts) {
            $RecoveryAttempt += 1
            Invoke-DispatchRecovery -Attempt $RecoveryAttempt
            continue
        }
        throw "Real-ESRGAN upscale failed with exit code $UpscaleExitCode."
    }
}
finally {
    try {
        & $ValidationManager -Action Stop -Service realesrgan -NonInteractive
    }
    finally {
        try {
            & $QueueCleanup -Service realesrgan -TimeoutSeconds 180 -NonInteractive
        }
        finally {
            & $ValidationManager -Action Status -Service realesrgan -NonInteractive
        }
    }
}
