[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Audio,
    [string]$AvatarImage = "",
    [ValidateSet("fast", "reference", "dev", "guided")][string]$Profile = "reference",
    [string]$SegmentId = "smoke-001",
    [string]$Prompt = "",
    [long]$Seed = 4242,
    [string]$OutputDir = "",
    [ValidateRange(0, 3600)][double]$MaxGenerationSeconds = 0,
    [string]$EnvFile = ".env",
    [ValidateRange(120, 21600)][int]$BootstrapTimeoutSeconds = 7200,
    [string]$ExpectedPinnedImage = "",
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Timers cover the complete controlled command, not just worker inference.
$LifecycleClock = [System.Diagnostics.Stopwatch]::StartNew()
$LifecycleMetrics = [ordered]@{
    schema_version = 1
    segment_id = $SegmentId
    profile = $Profile
    preflight_seconds = $null
    capacity_start_seconds = $null
    readiness_seconds = $null
    smoke_seconds = $null
    cleanup_seconds = $null
    end_to_end_seconds = $null
    outcome = "failed"
}

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$WorkerManager = Join-Path $RepoRoot "scripts\salad\manage_salad_worker.ps1"
$R2Preflight = Join-Path $RepoRoot "scripts\pipeline\check_r2_ready.py"
$Smoke = Join-Path $PSScriptRoot "submit_ltx25_a2v_smoke.py"
$ReadyWait = Join-Path $PSScriptRoot "wait_salad_ltx25_ready.py"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if ($Seed -lt 0) { throw "A2V seed must be non-negative." }
if (-not (Test-Path -LiteralPath $ReadyWait -PathType Leaf)) {
    throw "LTX worker readiness helper is missing: $ReadyWait"
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedPinnedImage) -and
    $ExpectedPinnedImage -notmatch "^[^\s@]+@sha256:[0-9a-fA-F]{64}$") {
    throw "-ExpectedPinnedImage must use immutable repo@sha256:digest form."
}
if ($Profile -eq "guided") {
    $Services = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\salad\services.json") -Raw | ConvertFrom-Json
    if ($Services.services.ltx25.environment.LTX_INCLUDE_A2V_DEV_ASSETS -ne "true") {
        throw "Guided A2V requires LTX_INCLUDE_A2V_DEV_ASSETS=true and a newly published/pinned worker image; refusing GPU allocation."
    }
}

if (-not (Test-Path -LiteralPath $Audio -PathType Leaf)) {
    throw "A2V smoke audio does not exist: $Audio"
}
if (-not [string]::IsNullOrWhiteSpace($AvatarImage) -and -not (Test-Path -LiteralPath $AvatarImage -PathType Leaf)) {
    throw "A2V smoke avatar image does not exist: $AvatarImage"
}

$ExpectedLtxModule = Join-Path $RepoRoot "src\ai_video_factory\workers\ltx25\__init__.py"
$SourceCheck = Join-Path $PSScriptRoot "check_ltx25_python_source.py"
& $Python $SourceCheck $ExpectedLtxModule
if ($LASTEXITCODE -ne 0) {
    throw "A2V smoke Python imports an outdated or different worktree. Run uv sync --locked --extra dev --python 3.12 in $RepoRoot before allocating a GPU."
}

& $Python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing LTX GPU allocation."
}

$Arguments = @(
    "--audio", $Audio,
    "--profile", $Profile,
    "--segment-id", $SegmentId,
    "--seed", $Seed
)
if (-not [string]::IsNullOrWhiteSpace($Prompt)) {
    $Arguments += @("--prompt", $Prompt)
}
if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
    $Arguments += @("--output-dir", $OutputDir)
}
if (-not [string]::IsNullOrWhiteSpace($AvatarImage)) {
    $Arguments += @("--avatar-image", $AvatarImage)
}
if ($MaxGenerationSeconds -gt 0) {
    $Arguments += @("--max-generation-seconds", $MaxGenerationSeconds)
}

$Start = @{
    Action = "Start"
    Service = "ltx25"
    Replicas = 1
    EnvFile = $EnvFile
    AllowBootstrappingInstance = $true
}
if ($NonInteractive) {
    $Start["NonInteractive"] = $true
}

$LifecycleMetrics.preflight_seconds = $LifecycleClock.Elapsed.TotalSeconds
$PhaseClock = [System.Diagnostics.Stopwatch]::StartNew()
$PhaseName = "capacity_start_seconds"

try {
    Write-Host "=== LTX A2V explicit capacity: one RTX 5090 replica ===" -ForegroundColor Cyan
    & $WorkerManager @Start
    if (-not $?) { throw "LTX A2V compute-group start failed." }
    $LifecycleMetrics.capacity_start_seconds = $PhaseClock.Elapsed.TotalSeconds
    $PhaseClock.Restart()
    $PhaseName = "readiness_seconds"

    # The fast-start signal means the container started, not that the model is ready.
    # Refuse Postgres submission until the same current-version instance passes /ready twice.
    $ReadyArgs = @(
        "--env-file", $EnvFile,
        "--timeout-seconds", [string]$BootstrapTimeoutSeconds,
        "--poll-seconds", "15"
    )
    if (-not [string]::IsNullOrWhiteSpace($ExpectedPinnedImage)) {
        $ReadyArgs += @("--expected-image", $ExpectedPinnedImage)
    }
    & $Python $ReadyWait @ReadyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "LTX worker did not become ready; no A2V job was submitted."
    }
    $LifecycleMetrics.readiness_seconds = $PhaseClock.Elapsed.TotalSeconds
    $PhaseClock.Restart()
    $PhaseName = "smoke_seconds"

    if ($Profile -in @("fast", "dev")) {
        Write-Warning "fast/dev is comparison-only: a valid MP4 is not lip-sync acceptance."
    }
    if ($Profile -eq "guided") {
        Write-Warning "guided is experimental: technical MP4/audio checks do not establish visual lip-sync."
    }
    & $Python $Smoke @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "LTX A2V smoke failed with exit code $LASTEXITCODE."
    }
    $LifecycleMetrics.smoke_seconds = $PhaseClock.Elapsed.TotalSeconds
    $PhaseName = ""
    $LifecycleMetrics.outcome = "generated"
}
finally {
    # Preserve the partial phase measurement when Start, /ready or the job fails.
    if ($PhaseName -and $null -eq $LifecycleMetrics[$PhaseName]) {
        $LifecycleMetrics[$PhaseName] = $PhaseClock.Elapsed.TotalSeconds
    }
    $CleanupClock = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        & $WorkerManager -Action Stop -Service ltx25 -EnvFile $EnvFile -NonInteractive
        if (-not $?) { throw "LTX A2V cleanup failed; verify stopped/replicas=0/pending=False." }
        if ($LifecycleMetrics.outcome -eq "generated") {
            $LifecycleMetrics.outcome = "succeeded"
        }
    }
    catch {
        $LifecycleMetrics.outcome = "cleanup_failed"
        throw
    }
    finally {
        $LifecycleMetrics.cleanup_seconds = $CleanupClock.Elapsed.TotalSeconds
        $LifecycleMetrics.end_to_end_seconds = $LifecycleClock.Elapsed.TotalSeconds
        Write-Host ("LTX25_A2V_LIFECYCLE_METRICS " + ($LifecycleMetrics | ConvertTo-Json -Compress -Depth 4))
    }
}
