[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Keyframes,

    [Parameter(Mandatory)]
    [string]$Prompts,

    [Parameter(Mandatory)]
    [string]$Timings,

    [Parameter(Mandatory)]
    [string]$OutputDir,

    [ValidateRange(10, 180)]
    [int]$PrewarmTimeoutMinutes = 90,

    [ValidateRange(60, 43200)]
    [int]$TimeoutSeconds = 21600,

    [ValidateRange(1, 60)]
    [int]$PollSeconds = 15,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ValidationManager = Join-Path $PSScriptRoot "manage_salad_validation.ps1"
$OptimizedPrewarm = Join-Path $PSScriptRoot "start_salad_optimized_prewarm.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"
$Runner = Join-Path $PSScriptRoot "run_phase8_videos.py"

foreach ($Path in @($Keyframes, $Prompts, $Timings)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Phase 8 input not found: $Path"
    }
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) {
    throw "R2 preflight failed; refusing to allocate LTX GPU."
}

$PrewarmArguments = @{
    Service = "ltx25"
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
}

try {
    Write-Host "=== LTX optimized prewarm: selecting one ready RTX5090 ===" -ForegroundColor Cyan
    & $OptimizedPrewarm @PrewarmArguments
    if (-not $?) {
        throw "LTX optimized prewarm failed."
    }

    Write-Host (
        "=== Phase 8 video generation: one worker ready; autoscaler may add workers ==="
    ) -ForegroundColor Cyan
    & python $Runner `
        --keyframes $Keyframes `
        --prompts $Prompts `
        --timings $Timings `
        --output-dir $OutputDir `
        --poll-seconds $PollSeconds `
        --timeout-seconds $TimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Phase 8 video generation failed with exit code $LASTEXITCODE."
    }
}
finally {
    try {
        & $ValidationManager `
            -Action Stop `
            -Service ltx25 `
            -NonInteractive
    }
    finally {
        & $ValidationManager `
            -Action Status `
            -Service ltx25 `
            -NonInteractive
    }
}
