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
    [int]$PrewarmTimeoutMinutes = 30,

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
$Runner = Join-Path $PSScriptRoot "run_phase5_alignment.py"

foreach ($Path in @($Source, $Narration, $Audio)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 5 alignment input not found: $Path"
    }
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
    & python $Runner `
        --source $Source `
        --narration $Narration `
        --audio $Audio `
        --output $Output `
        --poll-seconds $PollSeconds `
        --pending-timeout-seconds $PendingTimeoutSeconds `
        --timeout-seconds $RunningTimeoutSeconds
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
