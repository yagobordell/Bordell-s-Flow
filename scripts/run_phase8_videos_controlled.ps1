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
$ResumeSubmittedJobs = $false
if (Test-Path -LiteralPath $ManifestPath -PathType Leaf) {
    try {
        $ExistingManifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
        $SubmittedJobs = @(
            $ExistingManifest.jobs |
                Where-Object {
                    -not [string]::IsNullOrWhiteSpace([string]$_.transport_job_id)
                }
        )
        $ResumeSubmittedJobs = $SubmittedJobs.Count -gt 0
    }
    catch {
        throw (
            "Existing Phase 8 video generation manifest is unreadable; " +
            "refusing GPU allocation until it is repaired or archived. " +
            "Path=$ManifestPath Error=$($_.Exception.Message)"
        )
    }
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
        & python $QueueGuard ltx25 --output-dir $OutputDir
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
        --output-dir $OutputDir `
        --poll-seconds $PollSeconds `
        --timeout-seconds $TimeoutSeconds
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
