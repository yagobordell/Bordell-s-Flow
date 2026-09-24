[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SourceJobId,
    [string]$EnvFile = ".env",
    [string]$OutputDir = "data/output/qwen-bf16-recovery",
    [ValidateRange(60, 7200)][int]$TimeoutSeconds = 3600,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$WorkerManager = Join-Path $RepoRoot "scripts\salad\manage_salad_worker.ps1"
$Smoke = Join-Path $PSScriptRoot "run_qwen_bf16_recovery.py"
$R2Preflight = Join-Path $RepoRoot "scripts\pipeline\check_r2_ready.py"

Set-Location $RepoRoot
$Arguments = @(
    "--source-job-id", $SourceJobId,
    "--env-file", $EnvFile,
    "--output-dir", $OutputDir,
    "--timeout-seconds", [string]$TimeoutSeconds
)

Write-Host "=== R2 preflight (no GPU allocation) ===" -ForegroundColor Cyan
& uv run --no-sync python $R2Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; refusing Qwen GPU allocation." }

Write-Host "=== Postgres Qwen preflight (no GPU allocation) ===" -ForegroundColor Cyan
& uv run --no-sync python $Smoke @Arguments --preflight-only
if ($LASTEXITCODE -ne 0) { throw "Qwen preflight failed; refusing GPU allocation." }

try {
    Write-Host "=== Start one Qwen RTX 5090 replica ===" -ForegroundColor Cyan
    & $WorkerManager -Service qwen_image_21 -Action Start -Replicas 1 -EnvFile $EnvFile -NonInteractive:$NonInteractive
    if (-not $?) { throw "Qwen container group start failed." }

    Write-Host "=== Fresh BF16 comparison: exactly one attempt ===" -ForegroundColor Cyan
    & uv run --no-sync python $Smoke @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Qwen BF16 comparison failed with exit code $LASTEXITCODE." }
}
finally {
    Write-Host "=== Stop Qwen and converge to zero replicas ===" -ForegroundColor Cyan
    & $WorkerManager -Service qwen_image_21 -Action Stop -EnvFile $EnvFile -NonInteractive:$NonInteractive
}
