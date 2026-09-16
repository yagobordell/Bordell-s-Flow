[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Frames,

    [Parameter(Mandatory)]
    [string]$Shots,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [Parameter(Mandatory)]
    [string]$Output,

    [ValidateRange(10, 180)]
    [int]$PrewarmTimeoutMinutes = 90,

    [ValidateRange(30, 900)]
    [int]$PendingTimeoutSeconds = 300,

    [ValidateRange(60, 3600)]
    [int]$RunningTimeoutSeconds = 1200,

    [ValidateRange(1, 60)]
    [int]$PollSeconds = 5,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ValidationManager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$Phase6Runner = Join-Path $PSScriptRoot "run_phase6_keyframes.py"

foreach ($Path in @($Frames, $Shots)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 6 input not found: $Path"
    }
}

$PrewarmArguments = @{
    Action = "Prewarm"
    Service = "ideogram4"
    PrewarmTimeoutMinutes = $PrewarmTimeoutMinutes
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

try {
    Write-Host "=== Ideogram prewarm: waiting for one ready RTX4090 ===" -ForegroundColor Cyan
    & $ValidationManager @PrewarmArguments
    if (-not $?) {
        throw "Ideogram prewarm failed."
    }

    Write-Host "=== Phase 6 generation: worker is ready before queue submission ===" -ForegroundColor Cyan
    & python $Phase6Runner `
        --frames $Frames `
        --shots $Shots `
        --output-dir $OutputDir `
        --output $Output `
        --poll-seconds $PollSeconds `
        --pending-timeout-seconds $PendingTimeoutSeconds `
        --timeout-seconds $RunningTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 6 keyframe generation failed with exit code $LASTEXITCODE."
    }
}
finally {
    try {
        & $ValidationManager `
            -Action Stop `
            -Service ideogram4 `
            -NonInteractive
    }
    finally {
        & $ValidationManager `
            -Action Status `
            -Service ideogram4 `
            -NonInteractive
    }
}
