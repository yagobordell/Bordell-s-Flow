[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet("Phase 4", "Phase 6")][string]$Phase,
    [Parameter(Mandatory)][string[]]$RequiredInputs,
    [Parameter(Mandatory)][string]$CacheAuditScript,
    [Parameter(Mandatory)][string[]]$CacheAuditArguments,
    [Parameter(Mandatory)][string]$RunnerScript,
    [Parameter(Mandatory)][string[]]$RunnerArguments,
    [ValidateRange(10, 120)][int]$PrewarmTimeoutMinutes = 60,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$WorkerManager = Join-Path $PSScriptRoot "../salad/manage_salad_worker.ps1"
$R2Preflight = Join-Path $PSScriptRoot "check_r2_ready.py"

foreach ($Path in $RequiredInputs) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required $Phase input not found: $Path" }
}

Write-Host "=== R2 preflight: verify storage before GPU allocation ===" -ForegroundColor Cyan
& python $R2Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; refusing Qwen GPU allocation." }

$PlanPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-video-factory-qwen-plan-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
try {
    & python $CacheAuditScript @CacheAuditArguments --json-output $PlanPath
    if ($LASTEXITCODE -ne 0) { throw "$Phase cache planning failed." }
    $Plan = @(Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json | ForEach-Object { $_ })
}
finally {
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
}

$Hits = @($Plan | Where-Object { $_.status -eq "hit" }).Count
$Misses = @($Plan | Where-Object { $_.status -eq "miss" }).Count
$Invalid = @($Plan | Where-Object { $_.status -eq "invalid" }).Count
if ($Invalid -gt 0) { throw "$Phase cache contains invalid entries; refusing GPU allocation." }

if ($Misses -eq 0 -and $Hits -eq $Plan.Count) {
    Write-Host "$Phase is fully cached; no Qwen GPU allocation required." -ForegroundColor Green
    & python $RunnerScript @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "Verified $Phase replay failed to materialize locally." }
    return
}

$Start = @{ Action = "Start"; Service = "qwen_image_21"; Replicas = 1 }
$Stop = @{ Action = "Stop"; Service = "qwen_image_21" }
$Status = @{ Action = "Status"; Service = "qwen_image_21" }
if ($NonInteractive) {
    $Start["NonInteractive"] = $true
    $Stop["NonInteractive"] = $true
    $Status["NonInteractive"] = $true
}

try {
    Write-Host "=== $Phase Qwen capacity: one explicit replica ===" -ForegroundColor Cyan
    & $WorkerManager @Start
    if (-not $?) { throw "Qwen worker start failed." }

    & python $RunnerScript @RunnerArguments
    if ($LASTEXITCODE -ne 0) { throw "$Phase Qwen generation failed." }
}
finally {
    & $WorkerManager @Stop
    & $WorkerManager @Status
}
