[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Phase 4", "Phase 6")]
    [string]$Phase,

    [Parameter(Mandatory)]
    [string[]]$RequiredInputs,

    [Parameter(Mandatory)]
    [string]$CacheAuditScript,

    [Parameter(Mandatory)]
    [string[]]$CacheAuditArguments,

    [Parameter(Mandatory)]
    [string]$RunnerScript,

    [Parameter(Mandatory)]
    [string[]]$RunnerArguments,

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,

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
$QueueCleanup = Join-Path $PSScriptRoot "../salad/cleanup_salad_queue.ps1"

foreach ($Path in $RequiredInputs) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required $Phase input not found: $Path"
    }
}
Write-Host (
    "{0} canonical Salad route: qwen_image_21 queue={1}" -f
    $Phase, $env:SALAD_QWEN_IMAGE_21_QUEUE_NAME
) -ForegroundColor DarkGray

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate Qwen GPU."
}

$CachePlanPath = Join-Path ([IO.Path]::GetTempPath()) (
    "ai-video-factory-{0}-qwen-plan-{1}.json" -f
    ($Phase -replace "\s", "").ToLowerInvariant(), ([Guid]::NewGuid().ToString("N"))
)
try {
    & python $CacheAuditScript @CacheAuditArguments --json-output $CachePlanPath
    if ($LASTEXITCODE -ne 0) {
        throw "$Phase Qwen cache planning failed; refusing GPU allocation."
    }
    $Plan = @(Get-Content -LiteralPath $CachePlanPath -Raw | ConvertFrom-Json)
}
finally {
    Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
}

$Hits = @($Plan | Where-Object { $_.status -eq "hit" }).Count
$Misses = @($Plan | Where-Object { $_.status -eq "miss" }).Count
$Invalid = @($Plan | Where-Object { $_.status -eq "invalid" }).Count
if ($Invalid -gt 0) {
    throw "$Phase Qwen cache contains invalid entries; refusing GPU allocation."
}

$RunnerArguments = @($RunnerArguments) + @(
    "--queue-name", $env:SALAD_QWEN_IMAGE_21_QUEUE_NAME
)
if ($Misses -eq 0 -and $Hits -eq $Plan.Count) {
    Write-Host "$Phase is fully cached; no Qwen GPU allocation required." -ForegroundColor Green
    & python $RunnerScript @RunnerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Verified $Phase Qwen replay failed to materialize locally."
    }
    return
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
    & $OptimizedPrewarm @PrewarmArguments
    if (-not $?) { throw "$Phase Qwen prewarm failed." }

    & python $RunnerScript @RunnerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Phase Qwen generation failed."
    }
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
