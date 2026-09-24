[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Keyframes,
    [Parameter(Mandatory)][string]$Prompts,
    [Parameter(Mandatory)][string]$Timings,
    [Parameter(Mandatory)][string]$OutputDir,
    [ValidateRange(10, 180)][int]$PrewarmTimeoutMinutes = 90,
    [ValidateRange(60, 43200)][int]$TimeoutSeconds = 21600,
    [ValidateRange(30, 1800)][int]$DispatchTimeoutSeconds = 300,
    [ValidateRange(1, 60)][int]$PollSeconds = 15,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$WorkerManager = Join-Path $PSScriptRoot "../salad/manage_salad_worker.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$CacheAudit = Join-Path $PSScriptRoot "../diagnostics/audit_phase8_video_cache.py"
$Runner = Join-Path $PSScriptRoot "run_phase8_videos.py"
$ServicesPath = Join-Path $RepoRoot "deploy\salad\services.json"

foreach ($Path in @($Keyframes, $Prompts, $Timings)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required Phase 8 input not found: $Path" }
}

& python $R2Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; refusing LTX GPU allocation." }

$PlanPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-video-factory-ltx-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
try {
    & python $CacheAudit --keyframes $Keyframes --prompts $Prompts --timings $Timings --width 1280 --height 720 --fps 24 --json-output $PlanPath
    if ($LASTEXITCODE -ne 0) { throw "Phase 8 cache planning failed." }
    $Plan = @(Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json | ForEach-Object { $_ })
}
finally {
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
}

$Misses = @($Plan | Where-Object { $_.status -eq "miss" }).Count
$RunnerArguments = @(
    "--keyframes", $Keyframes,
    "--prompts", $Prompts,
    "--timings", $Timings,
    "--width", 1280,
    "--height", 720,
    "--fps", 24,
    "--output-dir", $OutputDir,
    "--poll-seconds", $PollSeconds,
    "--timeout-seconds", $TimeoutSeconds,
    "--dispatch-timeout-seconds", $DispatchTimeoutSeconds
)

if ($Misses -eq 0) {
    Write-Host "All Phase 8 clips are cached; no LTX GPU allocation required." -ForegroundColor Green
    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "Cached Phase 8 replay failed." }
    exit 0
}

$Services = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$MaxReplicas = [int]$Services.services.ltx25.capacity.max_replicas
$ReplicaCount = [Math]::Max(1, [Math]::Min($Misses, $MaxReplicas))
$Start = @{ Action = "Start"; Service = "ltx25"; Replicas = $ReplicaCount }
$Stop = @{ Action = "Stop"; Service = "ltx25" }
if ($NonInteractive) { $Start["NonInteractive"] = $true; $Stop["NonInteractive"] = $true }

try {
    Write-Host "=== LTX capacity: explicit replicas=$ReplicaCount for misses=$Misses ===" -ForegroundColor Cyan
    & $WorkerManager @Start
    if (-not $?) { throw "LTX worker start failed." }

    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "Phase 8 video generation failed with exit code $LASTEXITCODE." }
}
finally {
    & $WorkerManager @Stop
}
