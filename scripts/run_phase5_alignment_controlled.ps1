[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Source,

    [Parameter(Mandatory)]
    [string]$Narration,

    [Parameter(Mandatory)]
    [string]$Audio,

    [Parameter(Mandatory)]
    [string]$Output,

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,

    [ValidateRange(30, 900)]
    [int]$PendingTimeoutSeconds = 180,

    [ValidateRange(60, 3600)]
    [int]$RunningTimeoutSeconds = 900,

    [ValidateRange(1, 60)]
    [int]$PollSeconds = 5,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ValidationManager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$OptimizedPrewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$CacheAudit = Join-Path $PSScriptRoot "audit_phase5_alignment_cache.py"
$Runner = Join-Path $PSScriptRoot "run_phase5_alignment.py"
$ServicesPath = Join-Path (Split-Path $PSScriptRoot -Parent) "deploy\salad\services.json"

$Services = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$WhisperService = $Services.services.whisper
$env:SALAD_ORGANIZATION = [string]$Services.stack.organization
$env:SALAD_PROJECT = [string]$Services.stack.project
$env:SALAD_WHISPER_QUEUE_NAME = [string]$WhisperService.queue_name
Write-Host (
    "Phase 5 canonical Salad route: queue={0}" -f $env:SALAD_WHISPER_QUEUE_NAME
) -ForegroundColor DarkGray

foreach ($Path in @($Source, $Narration, $Audio)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 5 alignment input not found: $Path"
    }
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate Whisper GPU."
}

$CachePlanPath = Join-Path ([IO.Path]::GetTempPath()) (
    "ai-video-factory-phase5-whisper-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N"))
)
Write-Host "=== Phase 5 alignment cache: resolve replay before Whisper allocation ===" `
    -ForegroundColor Cyan
& python $CacheAudit `
    --source $Source `
    --audio $Audio `
    --json-output $CachePlanPath
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
    throw "Phase 5 Whisper cache planning failed; refusing Whisper GPU allocation."
}
$CachePlan = Get-Content -LiteralPath $CachePlanPath -Raw | ConvertFrom-Json
Remove-Item -LiteralPath $CachePlanPath -Force -ErrorAction SilentlyContinue
$WhisperCacheHit = [string]$CachePlan.status -eq "hit"

$RunnerArguments = @(
    "--source", $Source,
    "--narration", $Narration,
    "--audio", $Audio,
    "--output", $Output,
    "--queue-name", $env:SALAD_WHISPER_QUEUE_NAME,
    "--poll-seconds", $PollSeconds,
    "--pending-timeout-seconds", $PendingTimeoutSeconds,
    "--timeout-seconds", $RunningTimeoutSeconds
)

if ($WhisperCacheHit) {
    Write-Host (
        "Whisper alignment is a verified R2 replay; no Whisper GPU allocation required."
    ) -ForegroundColor Green
    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Verified Whisper replay failed to materialize locally."
    }
    exit 0
}

$PrewarmArguments = @{
    Service = "whisper"
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

try {
    Write-Host "=== Whisper optimized prewarm: selecting one ready node ===" -ForegroundColor Cyan
    & $OptimizedPrewarm @PrewarmArguments
    if (-not $?) {
        throw "Whisper optimized prewarm failed."
    }

    Write-Host "=== Phase 5 alignment: ready worker before queue submission ===" -ForegroundColor Cyan
    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 5 alignment failed with exit code $LASTEXITCODE."
    }
}
finally {
    try {
        & $ValidationManager `
            -Action Stop `
            -Service whisper `
            -NonInteractive
    }
    finally {
        & $ValidationManager `
            -Action Status `
            -Service whisper `
            -NonInteractive
    }
}
