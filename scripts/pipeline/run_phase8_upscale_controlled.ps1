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
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
. (Join-Path $RepoRoot "scripts\salad\_env_file.ps1")
Import-EnvFile -Path $EnvFile

$WorkerManager = Join-Path $PSScriptRoot "../salad/manage_salad_worker.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$CacheAudit = Join-Path $PSScriptRoot "../diagnostics/audit_phase8_upscale_cache.py"
$Runner = Join-Path $PSScriptRoot "run_phase8_upscale.py"
$ServicesPath = Join-Path $RepoRoot "deploy\salad\services.json"

if (-not (Test-Path -LiteralPath $Clips -PathType Leaf)) { throw "Required Phase 8 clips metadata not found: $Clips" }
& python $R2Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; refusing Real-ESRGAN GPU allocation." }

$PlanPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-video-factory-upscale-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
try {
    & python $CacheAudit --clips $Clips --json-output $PlanPath
    if ($LASTEXITCODE -ne 0) { throw "Real-ESRGAN cache planning failed." }
    $Plan = @(Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json | ForEach-Object { $_ })
}
finally {
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
}

$Misses = @($Plan | Where-Object { $_.status -eq "miss" }).Count
$RunnerArguments = @(
    "--clips", $Clips,
    "--output-dir", $OutputDir,
    "--poll-seconds", $PollSeconds,
    "--timeout-seconds", $TimeoutSeconds,
    "--dispatch-timeout-seconds", $DispatchTimeoutSeconds
)

if ($Misses -eq 0) {
    Write-Host "All upscaled clips are cached; no Real-ESRGAN GPU allocation required." -ForegroundColor Green
    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "Cached upscale replay failed." }
    exit 0
}

$Services = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$MaxReplicas = [int]$Services.services.realesrgan.capacity.max_replicas
$ReplicaCount = [Math]::Max(1, [Math]::Min($Misses, $MaxReplicas))
$Start = @{ Action = "Start"; Service = "realesrgan"; Replicas = $ReplicaCount; EnvFile = $EnvFile }
$Stop = @{ Action = "Stop"; Service = "realesrgan"; EnvFile = $EnvFile }
if ($NonInteractive) { $Start["NonInteractive"] = $true; $Stop["NonInteractive"] = $true }

try {
    Write-Host "=== Real-ESRGAN capacity: explicit replicas=$ReplicaCount for misses=$Misses ===" -ForegroundColor Cyan
    & $WorkerManager @Start
    if (-not $?) { throw "Real-ESRGAN worker start failed." }

    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "Phase 8 upscale failed with exit code $LASTEXITCODE." }
}
finally {
    & $WorkerManager @Stop
}
