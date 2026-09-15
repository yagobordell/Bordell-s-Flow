[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [string]$ComposeFile = "compose.yaml",

    [switch]$SkipLocalBuild,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Bootstrap = Join-Path $PSScriptRoot "start_salad_protected_smoke.ps1"
$Manager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$Service = "ltx25"
$ModelBootstrapTimeoutMinutes = 45
$RunningNotReadyReallocationThresholdMinutes = 60

foreach ($RequiredPath in @($Bootstrap, $Manager)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required protected-smoke helper not found: $RequiredPath"
    }
}

$BootstrapArguments = @{
    Service = $Service
    EnvFile = $EnvFile
    TimeoutMinutes = $ModelBootstrapTimeoutMinutes
    RunningNotReadyTimeoutMinutes = $RunningNotReadyReallocationThresholdMinutes
}
if ($NonInteractive) {
    $BootstrapArguments["NonInteractive"] = $true
}

$SmokeArguments = @{
    Action = "Smoke"
    Service = $Service
    EnvFile = $EnvFile
    ComposeFile = $ComposeFile
}
if ($SkipLocalBuild) {
    $SmokeArguments["SkipLocalBuild"] = $true
}
if ($NonInteractive) {
    $SmokeArguments["NonInteractive"] = $true
}

$StopArguments = @{
    Action = "Stop"
    Service = $Service
    EnvFile = $EnvFile
}
if ($NonInteractive) {
    $StopArguments["NonInteractive"] = $true
}

try {
    Write-Host (
        "=== Salad Cost-Guarded Protected Bootstrap : ltx25 " +
        "timeout=${ModelBootstrapTimeoutMinutes}m ==="
    ) -ForegroundColor Cyan
    & $Bootstrap @BootstrapArguments
    if (-not $?) {
        throw "LTX protected bootstrap failed."
    }

    & $Manager @SmokeArguments
    if (-not $?) {
        throw "LTX real smoke failed."
    }
}
finally {
    Write-Host "=== Salad Cost-Guarded Cleanup : ltx25 ===" -ForegroundColor Cyan
    & $Manager @StopArguments
    if (-not $?) {
        throw "LTX protected-smoke cleanup failed."
    }
}
