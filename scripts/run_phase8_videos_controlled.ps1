[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Keyframes,

    [Parameter(Mandatory)]
    [string]$Prompts,

    [Parameter(Mandatory)]
    [string]$Timings,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [ValidateRange(10, 180)]
    [int]$PrewarmTimeoutMinutes = 90,

    [ValidateRange(60, 43200)]
    [int]$TimeoutSeconds = 21600,

    [ValidateRange(30, 1800)]
    [int]$DispatchTimeoutSeconds = 300,

    [ValidateRange(1, 60)]
    [int]$PollSeconds = 15,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ValidationManager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$ScaleToZeroStarter = Join-Path $PSScriptRoot "start_salad_scale_to_zero.ps1"
$OptimizedPrewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$QueueGuard = Join-Path $PSScriptRoot "check_salad_queue_ready.py"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$CacheAudit = Join-Path $PSScriptRoot "audit_phase8_video_cache.py"
$ManifestInspector = Join-Path $PSScriptRoot "inspect_phase8_manifest.py"
$Runner = Join-Path $PSScriptRoot "run_phase8_videos.py"
$ServicesPath = Join-Path (Split-Path $PSScriptRoot -Parent) "deploy\salad\services.json"

foreach ($Path in @($Keyframes, $Prompts, $Timings)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 8 input not found: $Path"
    }
}

$Services = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$LtxService = $Services.services.ltx25
$env:SALAD_ORGANIZATION = [string]$Services.stack.organization
$env:SALAD_PROJECT = [string]$Services.stack.project
$env:SALAD_LTX25_QUEUE_NAME = [string]$LtxService.queue_name
Write-Host (
    "Phase 8 canonical Salad route: queue={0}" -f $env:SALAD_LTX25_QUEUE_NAME
) -ForegroundColor DarkGray

$ManifestPath = Join-Path $OutputDir "video_generation_manifest.json"
$ProductionOutputRoot = Split-Path -Parent $OutputDir
if ([string]::IsNullOrWhiteSpace($ProductionOutputRoot)) {
    $ProductionOutputRoot = "."
}

$ManifestStatePath = Join-Path ([IO.Path]::GetTempPath()) (
    "ai-video-factory-phase8-manifest-{0}.json" -f ([Guid]::NewGuid().ToString("N"))
)
Write-Host "=== Phase 8 resume manifest: verify current deterministic plan ===" `
    -ForegroundColor Cyan
& python $ManifestInspector `
    --keyframes $Keyframes `
    --prompts $Prompts `
    --timings $Timings `
    --width 1280 `
    --height 720 `
    --fps 24 `
    --manifest $ManifestPath `
    --json-output $ManifestStatePath
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $ManifestStatePath -Force -ErrorAction SilentlyContinue
    throw "Phase 8 resume manifest inspection failed; refusing GPU allocation."
}
$ManifestState = Get-Content -LiteralPath $ManifestStatePath -Raw | ConvertFrom-Json
Remove-Item -LiteralPath $ManifestStatePath -Force -ErrorAction SilentlyContinue

$ResumeSubmittedJobs = (
    [string]$ManifestState.status -eq "matching" -and
    [int]$ManifestState.active_resume_jobs -gt 0
)

if ([string]$ManifestState.status -eq "different_plan") {
    Write-Host (
        "=== Phase 8 stale manifest: verify no active LTX work before archival ==="
    ) -ForegroundColor Cyan
    $EmptyGuardRoot = Join-Path ([IO.Path]::GetTempPath()) (
        "ai-video-factory-phase8-empty-guard-{0}" -f ([Guid]::NewGuid().ToString("N"))
    )
    New-Item -ItemType Directory -Path $EmptyGuardRoot -Force | Out-Null
    try {
        & python $QueueGuard ltx25 --output-dir $EmptyGuardRoot
        if ($LASTEXITCODE -ne 0) {
            throw (
                "Existing Phase 8 manifest belongs to a different plan and the LTX queue " +
                "is not provably idle; refusing to archive it or allocate GPU."
            )
        }
    }
    finally {
        Remove-Item -LiteralPath $EmptyGuardRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    $ArchiveStatePath = Join-Path ([IO.Path]::GetTempPath()) (
        "ai-video-factory-phase8-archive-{0}.json" -f ([Guid]::NewGuid().ToString("N"))
    )
    & python $ManifestInspector `
        --keyframes $Keyframes `
        --prompts $Prompts `
        --timings $Timings `
        --width 1280 `
        --height 720 `
        --fps 24 `
        --manifest $ManifestPath `
        --json-output $ArchiveStatePath `
        --archive-mismatch
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $ArchiveStatePath -Force -ErrorAction SilentlyContinue
        throw "Phase 8 stale manifest archival failed; refusing GPU allocation."
    }
    $ArchiveState = Get-Content -LiteralPath $ArchiveStatePath -Raw | ConvertFrom-Json
    Remove-Item -LiteralPath $ArchiveStatePath -Force -ErrorAction SilentlyContinue
    if ([string]$ArchiveState.status -ne "archived_different_plan") {
        throw "Phase 8 stale manifest changed unexpectedly during archival."
    }
    $ResumeSubmittedJobs = $false
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate LTX GPU."
}

$CachePlanPath = Join-Path ([IO.Path]::GetTempPath()) (
    "ai-video-factory-phase8-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N"))
)
Write-Host "=== Phase 8 cache plan: resolve R2 replay before LTX allocation ===" -ForegroundColor Cyan
& python $CacheAudit `
    --keyframes $Keyframes `
    --prompts $Prompts `
    --timings $Timings `
    --width 1280 `
    --height 720 `
    --fps 24 `
    --json-output $CachePlanPath
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
    throw "Phase 8 cache planning failed; refusing LTX GPU allocation."
}
$CachePlan = @(
    Get-Content -LiteralPath $CachePlanPath -Raw |
        ConvertFrom-Json |
        ForEach-Object { $_ }
)
Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
$InvalidCache = @($CachePlan | Where-Object { $_.status -eq "invalid" }).Count
if ($InvalidCache -gt 0) {
    throw "Phase 8 cache contains $InvalidCache invalid artifact(s); refusing GPU allocation."
}
$LtxNeeded = @($CachePlan | Where-Object { $_.status -eq "miss" }).Count -gt 0
Write-Host (
    "Phase 8 GPU plan: ltx25={0} cached={1} misses={2}" -f `
    $LtxNeeded,
    @($CachePlan | Where-Object { $_.status -eq "hit" }).Count,
    @($CachePlan | Where-Object { $_.status -eq "miss" }).Count
) -ForegroundColor Green

$PrewarmArguments = @{
    Service = "ltx25"
    TimeoutMinutes = $PrewarmTimeoutMinutes
    HoldReadyReplica = $true
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

try {
    if (-not $LtxNeeded) {
        Write-Host (
            "All Phase 8 clips are valid R2 replays; LTX GPU allocation is skipped."
        ) -ForegroundColor Green
    }
    elseif ($ResumeSubmittedJobs) {
        Write-Host (
            "=== LTX resume: verify queue ownership before any GPU allocation ==="
        ) -ForegroundColor Cyan
        & python $QueueGuard ltx25 --output-dir $ProductionOutputRoot
        if ($LASTEXITCODE -ne 0) {
            throw "LTX resume queue ownership guard failed; refusing GPU allocation."
        }

        Write-Host (
            "=== LTX resume: start scale-to-zero group for existing transport jobs ==="
        ) -ForegroundColor Cyan
        $StartArguments = @{
            Service = "ltx25"
        }
        if ($NonInteractive) {
            $StartArguments["NonInteractive"] = $true
        }
        & $ScaleToZeroStarter @StartArguments
        if (-not $?) {
            throw "LTX resume scale-to-zero start failed."
        }
    }
    else {
        Write-Host "=== LTX optimized prewarm: selecting one ready RTX5090 ===" -ForegroundColor Cyan
        & $OptimizedPrewarm @PrewarmArguments
        if (-not $?) {
            throw "LTX optimized prewarm failed."
        }
    }

    Write-Host (
        "=== Phase 8 video generation: worker group available; resume/fanout active ==="
    ) -ForegroundColor Cyan
    & python $Runner `
        --keyframes $Keyframes `
        --prompts $Prompts `
        --timings $Timings `
        --width 1280 `
        --height 720 `
        --fps 24 `
        --output-dir $OutputDir `
        --poll-seconds $PollSeconds `
        --timeout-seconds $TimeoutSeconds `
        --dispatch-timeout-seconds $DispatchTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 8 video generation failed with exit code $LASTEXITCODE."
    }
}
finally {
    try {
        & $ValidationManager `
            -Action Stop `
            -Service ltx25 `
            -NonInteractive
    }
    finally {
        & $ValidationManager `
            -Action Status `
            -Service ltx25 `
            -NonInteractive
    }
}
