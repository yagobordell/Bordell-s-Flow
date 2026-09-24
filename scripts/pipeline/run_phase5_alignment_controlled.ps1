[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Source,
    [Parameter(Mandatory)][string]$Narration,
    [Parameter(Mandatory)][string]$Audio,
    [Parameter(Mandatory)][string]$Output,
    [ValidatePattern("^[A-Za-z][A-Za-z0-9_-]{1,15}$")][string]$Language = "en",
    [ValidateRange(10, 120)][int]$PrewarmTimeoutMinutes = 120,
    [ValidateRange(30, 900)][int]$PendingTimeoutSeconds = 180,
    [ValidateRange(60, 3600)][int]$RunningTimeoutSeconds = 900,
    [ValidateRange(1, 60)][int]$PollSeconds = 5,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$WorkerManager = Join-Path $PSScriptRoot "../salad/manage_salad_worker.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$CacheAudit = Join-Path $PSScriptRoot "../diagnostics/audit_phase5_alignment_cache.py"
$Runner = Join-Path $PSScriptRoot "run_phase5_alignment.py"

foreach ($Path in @($Source, $Narration, $Audio)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required Phase 5 alignment input not found: $Path" }
}

& python $R2Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; refusing Whisper GPU allocation." }

$PlanPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-video-factory-whisper-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
try {
    & python $CacheAudit --source $Source --audio $Audio --language $Language --json-output $PlanPath
    if ($LASTEXITCODE -ne 0) { throw "Whisper cache planning failed." }
    $Plan = Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
}
finally {
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
}

$RunnerArguments = @(
    "--source", $Source,
    "--narration", $Narration,
    "--audio", $Audio,
    "--language", $Language,
    "--output", $Output,
    "--poll-seconds", $PollSeconds,
    "--pending-timeout-seconds", $PendingTimeoutSeconds,
    "--timeout-seconds", $RunningTimeoutSeconds
)

if ([string]$Plan.status -eq "hit") {
    Write-Host "Whisper alignment is cached; no GPU allocation required." -ForegroundColor Green
    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "Verified Whisper replay failed." }
    exit 0
}

$Start = @{ Action = "Start"; Service = "whisper"; Replicas = 1 }
$Stop = @{ Action = "Stop"; Service = "whisper" }
if ($NonInteractive) { $Start["NonInteractive"] = $true; $Stop["NonInteractive"] = $true }

try {
    & $WorkerManager @Start
    if (-not $?) { throw "Whisper worker start failed." }
    & python $Runner @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "Phase 5 alignment failed with exit code $LASTEXITCODE." }
}
finally {
    & $WorkerManager @Stop
}
