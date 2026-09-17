[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SourceFile,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [Parameter(Mandatory)]
    [string]$Metadata,

    [ValidateRange(10, 120)]
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
$OptimizedPrewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$Runner = Join-Path $PSScriptRoot "run_phase5_audio.py"

if (-not (Test-Path -LiteralPath $SourceFile -PathType Leaf)) {
    throw "Phase 5 source script not found: $SourceFile"
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate Breeze GPU."
}

$PrewarmArguments = @{
    Service = "breeze_tts2"
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

try {
    Write-Host "=== Breeze optimized prewarm: selecting one ready node ===" -ForegroundColor Cyan
    & $OptimizedPrewarm @PrewarmArguments
    if (-not $?) {
        throw "Breeze optimized prewarm failed."
    }

    Write-Host "=== Phase 5 narration: ready worker before queue submission ===" -ForegroundColor Cyan
    & python $Runner `
        $SourceFile `
        --output-dir $OutputDir `
        --metadata $Metadata `
        --poll-seconds $PollSeconds `
        --pending-timeout-seconds $PendingTimeoutSeconds `
        --timeout-seconds $RunningTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 5 narration failed with exit code $LASTEXITCODE."
    }
}
finally {
    try {
        & $ValidationManager `
            -Action Stop `
            -Service breeze_tts2 `
            -NonInteractive
    }
    finally {
        & $ValidationManager `
            -Action Status `
            -Service breeze_tts2 `
            -NonInteractive
    }
}
