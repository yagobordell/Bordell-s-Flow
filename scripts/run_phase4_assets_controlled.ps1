[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ReferencesFile,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [Parameter(Mandatory)]
    [string]$Metadata,

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,

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
$OptimizedPrewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$Phase4Runner = Join-Path $PSScriptRoot "run_phase4_assets.py"

if (-not (Test-Path -LiteralPath $ReferencesFile -PathType Leaf)) {
    throw "Visual references file not found: $ReferencesFile"
}

$PrewarmArguments = @{
    Service = "ideogram4"
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

try {
    Write-Host "=== Ideogram optimized prewarm: selecting one ready node ===" -ForegroundColor Cyan
    & $OptimizedPrewarm @PrewarmArguments
    if (-not $?) {
        throw "Ideogram optimized prewarm failed."
    }

    Write-Host "=== Phase 4 generation: ready worker before queue submission ===" -ForegroundColor Cyan
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
