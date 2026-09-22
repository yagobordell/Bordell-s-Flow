[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AvatarImage,

    [Parameter(Mandatory = $true)]
    [string]$Audio,

    [string]$Prompt = "",

    [string]$EnvFile = ".env",

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,

    [switch]$KeepRunning,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Prewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$Validation = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$Cleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$ZeroGuard = Join-Path $PSScriptRoot "ensure_salad_zero_replicas.ps1"
$Smoke = Join-Path $PSScriptRoot "submit_ltx25_a2v_smoke.py"

foreach ($RequiredPath in @($Prewarm, $Validation, $Cleanup, $ZeroGuard, $Smoke)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required A2V smoke helper not found: $RequiredPath"
    }
}

$PrewarmArguments = @{
    Service = "ltx25"
    EnvFile = $EnvFile
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

$SmokeArguments = @(
    "--env-file", $EnvFile,
    "--avatar-image", $AvatarImage,
    "--audio", $Audio
)
if (-not [string]::IsNullOrWhiteSpace($Prompt)) {
    $SmokeArguments += @("--prompt", $Prompt)
}

function Stop-LtxA2VAndVerify {
    & $Validation -Action Stop -Service ltx25 -EnvFile $EnvFile -NonInteractive
    if (-not $?) {
        throw "Failed to stop LTX 2.5 Salad service."
    }
    & $Cleanup -Service ltx25 -EnvFile $EnvFile -NonInteractive
    if (-not $?) {
        throw "Failed to clean LTX 2.5 Salad queue."
    }
    & $ZeroGuard -Service ltx25 -EnvFile $EnvFile -NonInteractive
    if (-not $?) {
        throw "LTX 2.5 Salad service did not settle at replicas=0."
    }
}

try {
    Write-Host "=== LTX 2.5 A2V prewarm: RTX 5090 / priority high ===" -ForegroundColor Cyan
    & $Prewarm @PrewarmArguments
    if (-not $?) {
        throw "LTX A2V prewarm failed."
    }

    Write-Host "=== LTX 2.5 A2V real image + speech inference ===" -ForegroundColor Cyan
    & python $Smoke @SmokeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "LTX A2V real smoke failed with exit code $LASTEXITCODE."
    }
}
finally {
    if (-not $KeepRunning) {
        Write-Host "=== LTX 2.5 A2V scale-to-zero cleanup ===" -ForegroundColor Cyan
        Stop-LtxA2VAndVerify
    }
    else {
        Write-Host (
            "LTX 2.5 service left running by -KeepRunning. " +
            "Stop later with manage_salad_validation.ps1 -Action Stop -Service ltx25."
        ) -ForegroundColor Yellow
    }
}
