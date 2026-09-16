[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ReferencesFile,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [Parameter(Mandatory)]
    [string]$Metadata,

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

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ValidationManager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$Phase4Runner = Join-Path $PSScriptRoot "run_phase4_assets.py"

if (-not (Test-Path -LiteralPath $ReferencesFile -PathType Leaf)) {
    throw "Visual references file not found: $ReferencesFile"
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

    Write-Host "=== Phase 4 generation: worker is ready before queue submission ===" -ForegroundColor Cyan
    & python $Phase4Runner `
        $ReferencesFile `
        --output-dir $OutputDir `
        --metadata $Metadata `
        --poll-seconds $PollSeconds `
        --pending-timeout-seconds $PendingTimeoutSeconds `
        --timeout-seconds $RunningTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 4 asset generation failed with exit code $LASTEXITCODE."
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
