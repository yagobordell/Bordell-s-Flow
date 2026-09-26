[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Audio,
    [string]$AvatarImage = "",
    [ValidateSet("fast", "reference", "reference-compiled", "dev", "guided")][string]$Profile = "reference",
    [string]$SegmentId = "smoke-001",
    [string]$Prompt = "",
    [long]$Seed = 4242,
    [string]$OutputDir = "",
    [ValidateRange(0, 3600)][double]$MaxGenerationSeconds = 0,
    [string]$EnvFile = ".env",
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$WorkerManager = Join-Path $RepoRoot "scripts\salad\manage_salad_worker.ps1"
$R2Preflight = Join-Path $RepoRoot "scripts\pipeline\check_r2_ready.py"
$Smoke = Join-Path $PSScriptRoot "submit_ltx25_a2v_smoke.py"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if ($Seed -lt 0) { throw "A2V seed must be non-negative." }
if ($Profile -eq "reference-compiled") {
    $Services = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\salad\services.json") -Raw | ConvertFrom-Json
    if ($Services.services.ltx25.image -notmatch "-compiled-") {
        throw "Compiled reference A2V requires a newly versioned compiled worker image in the tracked Salad manifest; current image cannot run this profile. Refusing GPU allocation."
    }
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
}
if ($NonInteractive) {
    $Start["NonInteractive"] = $true
}

try {
    Write-Host "=== LTX A2V explicit capacity: one RTX 5090 replica ===" -ForegroundColor Cyan
    & $WorkerManager @Start
    if (-not $?) { throw "LTX A2V compute-group start failed." }

    if ($Profile -in @("fast", "dev")) {
        Write-Warning "fast/dev is comparison-only: a valid MP4 is not lip-sync acceptance."
    }
    if ($Profile -eq "guided") {
        Write-Warning "guided is experimental: technical MP4/audio checks do not establish visual lip-sync."
    }
    if ($Profile -eq "reference-compiled") {
        Write-Warning "reference-compiled is an unvalidated A/B experiment. Cold compilation may be slower; review lip-sync and frames before promotion."
    }
    & $Python $Smoke @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "LTX A2V smoke failed with exit code $LASTEXITCODE."
    }
}
finally {
    & $WorkerManager -Action Stop -Service ltx25 -EnvFile $EnvFile -NonInteractive
}
