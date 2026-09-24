[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Audio,
    [string]$AvatarImage = "",
    [ValidateSet("fast", "dev")][string]$Profile = "fast",
    [string]$SegmentId = "smoke-001",
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

if (-not (Test-Path -LiteralPath $Audio -PathType Leaf)) {
    throw "A2V smoke audio does not exist: $Audio"
}
if (-not [string]::IsNullOrWhiteSpace($AvatarImage) -and -not (Test-Path -LiteralPath $AvatarImage -PathType Leaf)) {
    throw "A2V smoke avatar image does not exist: $AvatarImage"
}

& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing LTX GPU allocation."
}

$Arguments = @(
    "--audio", $Audio,
    "--profile", $Profile,
    "--segment-id", $SegmentId
)
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

    & python $Smoke @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "LTX A2V smoke failed with exit code $LASTEXITCODE."
    }
}
finally {
    & $WorkerManager -Action Stop -Service ltx25 -EnvFile $EnvFile -NonInteractive
}
