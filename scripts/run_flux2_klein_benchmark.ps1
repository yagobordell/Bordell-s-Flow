[CmdletBinding()]
param(
    [string]$EnvFile = ".env",
    [string]$OutputDir = "data/output/deployment-validation/flux2-klein-benchmark",
    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,
    [ValidateRange(300, 7200)]
    [int]$InferenceTimeoutSeconds = 1800,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$Prewarm = Join-Path $PSScriptRoot "start_salad_flux2_klein_prewarm.ps1"
$Restore = Join-Path $PSScriptRoot "restore_salad_flux2_klein_scale_to_zero.ps1"
$Cleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$WorkerManager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$Smoke = Join-Path $PSScriptRoot "run_salad_smoke_suite.py"

function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Path)
    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path $RepoRoot $Resolved
    }
    if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
        return
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
        $Value = $Matches["value"].Trim().Trim('"').Trim("'")
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name))) {
            [Environment]::SetEnvironmentVariable($Name, $Value)
        }
    }
}

function Get-RequiredEnvironment {
    param([Parameter(Mandatory)][string]$Name)
    $Value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is required for the FLUX.2 Klein benchmark."
    }
    return $Value.Trim()
}

Import-EnvFile -Path $EnvFile
$OutputPath = $OutputDir
if (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $RepoRoot $OutputPath
}
New-Item -ItemType Directory -Force -Path $OutputPath | Out-Null
$ColdStartMetrics = Join-Path $OutputPath "cold-start.json"
$SmokeDir = Join-Path $OutputPath "smoke"
$BenchmarkPath = Join-Path $OutputPath "benchmark.json"

Write-Host "=== FLUX.2 Klein benchmark: R2 preflight ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed before FLUX.2 Klein benchmark."
}

$PrimaryFailure = $null
$CleanupFailures = @()
try {
    Write-Host "=== FLUX.2 Klein benchmark: cold start to ready ===" -ForegroundColor Cyan
    $PrewarmArgs = @{
        EnvFile = $EnvFile
        TimeoutMinutes = $PrewarmTimeoutMinutes
        MetricsOutput = $ColdStartMetrics
    }
    if ($NonInteractive) {
        $PrewarmArgs["NonInteractive"] = $true
    }
    & $Prewarm @PrewarmArgs
    if (-not $?) {
        throw "FLUX.2 Klein benchmark prewarm failed."
    }

    Write-Host "=== FLUX.2 Klein benchmark: real 1024x1024 queue inference ===" -ForegroundColor Cyan
    & python $Smoke --service flux2_klein --output-dir $SmokeDir --timeout-seconds $InferenceTimeoutSeconds --poll-seconds 2
    if ($LASTEXITCODE -ne 0) {
        throw "FLUX.2 Klein real smoke failed."
    }
}
catch {
    $PrimaryFailure = $_
}
finally {
    try {
        & $Restore -EnvFile $EnvFile -TimeoutSeconds 300 -NonInteractive
    }
    catch {
        Write-Warning "FLUX.2 Klein scale-to-zero restore failed: $($_.Exception.Message)"
        $CleanupFailures += $_
    }
    try {
        & $Cleanup -Service flux2_klein -EnvFile $EnvFile -TimeoutSeconds 300 -NonInteractive
    }
    catch {
        Write-Warning "FLUX.2 Klein queue cleanup failed: $($_.Exception.Message)"
        $CleanupFailures += $_
    }
    try {
        & $WorkerManager -Action Status -Service flux2_klein -EnvFile $EnvFile -NonInteractive
    }
    catch {
        Write-Warning "FLUX.2 Klein final status check failed: $($_.Exception.Message)"
        $CleanupFailures += $_
    }
}

if ($null -ne $PrimaryFailure) {
    throw $PrimaryFailure
}
if ($CleanupFailures.Count -gt 0) {
    throw $CleanupFailures[0]
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$Definition = $Manifest.services.flux2_klein
$ApiKey = Get-RequiredEnvironment -Name "SALAD_API_KEY"
$BaseUrl = (
    "https://api.salad.com/api/public/organizations/{0}/projects/{1}" -f
    [string]$Manifest.stack.organization,
    [string]$Manifest.stack.project
)
$Headers = @{
    "Salad-Api-Key" = $ApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-flux2-klein-benchmark/1.0"
}
$Group = Invoke-RestMethod -Uri "$BaseUrl/containers/$([string]$Definition.group_name)" -Headers $Headers -TimeoutSec 30
$Queue = Invoke-RestMethod -Uri "$BaseUrl/queues/$([string]$Definition.queue_name)" -Headers $Headers -TimeoutSec 30

$GroupStatus = [string]$Group.current_state.status
$Replicas = [int]$Group.replicas
if ($GroupStatus -ne "stopped" -or $Replicas -ne 0 -or [bool]$Group.pending_change) {
    throw (
        "FLUX.2 Klein benchmark cleanup invariant failed: " +
        "status=$GroupStatus replicas=$Replicas pending=$([bool]$Group.pending_change)"
    )
}
if ([int]$Queue.current_queue_length -ne 0) {
    throw "FLUX.2 Klein benchmark left queue work behind."
}

$Cold = Get-Content -LiteralPath $ColdStartMetrics -Raw | ConvertFrom-Json
$SmokeReportPath = Join-Path $SmokeDir "flux2_klein-smoke.json"
$SmokeReport = Get-Content -LiteralPath $SmokeReportPath -Raw | ConvertFrom-Json
$ArtifactPath = Join-Path $SmokeDir "flux2-klein-smoke.png"
$Benchmark = [ordered]@{
    schema_version = "1"
    service = "flux2_klein"
    model = [string]$SmokeReport.model
    model_revision = [string]$Definition.environment.FLUX2_KLEIN_MODEL_REVISION
    image = [string]$Group.container.image
    group = [string]$Definition.group_name
    queue = [string]$Definition.queue_name
    resources = $Definition.resources
    node_assignment_seconds = [double]$Cold.node_assignment_seconds
    docker_image_download_seconds = [double]$Cold.image_download_seconds
    docker_image_download_state_observed = [bool]$Cold.image_download_state_observed
    model_bootstrap_seconds = [double]$Cold.model_bootstrap_seconds
    time_to_ready_seconds = [double]$Cold.time_to_ready_seconds
    generation_r2_roundtrip_seconds = [double]$SmokeReport.wall_seconds
    vram_peak_mib = $null
    vram_note = "Salad public control plane does not expose per-process peak VRAM; BFL documents ~13GB for BF16 Klein 4B."
    output = [ordered]@{
        width = [int]$SmokeReport.width
        height = [int]$SmokeReport.height
        size_bytes = [int64]$SmokeReport.size_bytes
        sha256 = [string]$SmokeReport.sha256
        path = $ArtifactPath
    }
    r2_upload_download = "succeeded"
    cleanup = [ordered]@{
        group_status = $GroupStatus
        replicas = $Replicas
        pending_change = [bool]$Group.pending_change
        queue_length = [int]$Queue.current_queue_length
    }
}
$Benchmark | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $BenchmarkPath -Encoding utf8
Write-Host "FLUX.2 Klein benchmark complete: $BenchmarkPath" -ForegroundColor Green
Get-Content -LiteralPath $BenchmarkPath
