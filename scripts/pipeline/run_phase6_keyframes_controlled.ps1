[CmdletBinding()]
param(
    [string]$Frames = "data/output/phase6/storyboard_frames.json",
    [string]$Shots = "data/output/phase3/shots.json",
    [string]$OutputDir = "data/output/phase6/storyboard_keyframes",
    [string]$Output = "data/output/phase6/storyboard_keyframes.json",
    [ValidateRange(30, 3600)]
    [int]$PendingTimeoutSeconds = 1800,
    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,
    [ValidateRange(60, 7200)]
    [int]$RunningTimeoutSeconds = 3600,
    [ValidateRange(1, 60)]
    [int]$PollSeconds = 5,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ServicesPath = Join-Path $RepoRoot "deploy\salad\services.json"
$Services = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$QwenService = $Services.services.qwen_image_21
$env:SALAD_ORGANIZATION = [string]$Services.stack.organization
$env:SALAD_PROJECT = [string]$Services.stack.project
$env:SALAD_QWEN_IMAGE_21_QUEUE_NAME = [string]$QwenService.queue_name

$ValidationManager = Join-Path $PSScriptRoot "../salad/manage_salad_validation.ps1"
$OptimizedPrewarm = Join-Path $PSScriptRoot "../salad/start_salad_optimized_prewarm.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$CacheAudit = Join-Path $PSScriptRoot "../diagnostics/audit_phase6_keyframe_cache.py"
$QueueCleanup = Join-Path $PSScriptRoot "../salad/cleanup_salad_queue.ps1"
$Runner = Join-Path $PSScriptRoot "run_phase6_keyframes.py"

foreach ($Path in @($Frames, $Shots)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 6 input not found: $Path"
    }
}

Write-Host (
    "Phase 6 canonical Salad route: qwen_image_21 queue={0}" -f
    $env:SALAD_QWEN_IMAGE_21_QUEUE_NAME
) -ForegroundColor DarkGray

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate Qwen GPU."
}

$CachePlanPath = Join-Path ([IO.Path]::GetTempPath()) (
    "ai-video-factory-phase6-qwen-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N"))
)
Write-Host "=== Phase 6 cache plan: resolve Qwen replay before allocation ===" -ForegroundColor Cyan
& python $CacheAudit $Frames --json-output $CachePlanPath
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
    throw "Phase 6 Qwen cache planning failed; refusing GPU allocation."
}
$Plan = @(
    Get-Content -LiteralPath $CachePlanPath -Raw |
        ConvertFrom-Json |
        ForEach-Object { $_ }
)
Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
$Hits = @($Plan | Where-Object { $_.status -eq "hit" }).Count
$Misses = @($Plan | Where-Object { $_.status -eq "miss" }).Count
$Invalid = @($Plan | Where-Object { $_.status -eq "invalid" }).Count
if ($Invalid -gt 0) {
    throw "Phase 6 Qwen cache contains invalid entries; refusing GPU allocation."
}

$RunnerArguments = @(
    "--frames", $Frames,
    "--shots", $Shots,
    "--queue-name", $env:SALAD_QWEN_IMAGE_21_QUEUE_NAME,
    "--poll-seconds", $PollSeconds,
    "--pending-timeout-seconds", $PendingTimeoutSeconds,
    "--timeout-seconds", $RunningTimeoutSeconds,
    "--output-dir", $OutputDir,
    "--output", $Output
)

if ($Misses -eq 0 -and $Hits -eq $Plan.Count) {
    Write-Host "All Phase 6 keyframes are valid Qwen R2 replays; no GPU allocation required." -ForegroundColor Green
    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Verified Phase 6 Qwen replay failed to materialize locally."
    }
    exit 0
}

$PrewarmArguments = @{
    Service = "qwen_image_21"
    TimeoutMinutes = $PrewarmTimeoutMinutes
    HoldReadyReplica = $true
    AllowScaleToZeroFallback = $true
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

try {
    Write-Host "=== Phase 6 Qwen-Image-2.1 prewarm ===" -ForegroundColor Cyan
    & $OptimizedPrewarm @PrewarmArguments
    if (-not $?) { throw "Qwen-Image-2.1 prewarm failed." }

    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "Phase 6 Qwen keyframe generation failed." }
}
finally {
    try {
        & $ValidationManager -Action Stop -Service qwen_image_21 -NonInteractive
    }
    finally {
        try {
            & $QueueCleanup -Service qwen_image_21 -TimeoutSeconds 180 -NonInteractive
        }
        finally {
            & $ValidationManager -Action Status -Service qwen_image_21 -NonInteractive
        }
    }
}
