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

Write-Host "=== Verify BF16 image and stopped Salad group (no GPU allocation) ===" -ForegroundColor Cyan
$Manifest = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\\salad\\services.json") -Raw | ConvertFrom-Json
$Qwen = $Manifest.services.qwen_image_21
if ([string]$Qwen.environment.QWEN_IMAGE_21_MEMORY_MODE -ne "bf16_offload") {
    throw "The Qwen deployment manifest is not set to bf16_offload."
}
$Published = & docker buildx imagetools inspect ([string]$Qwen.image) 2>&1
if ($LASTEXITCODE -ne 0) { throw "BF16 image was not published successfully." }
$Digest = $null
foreach ($Line in $Published) {
    if ([string]$Line -match '^\\s*Digest:\\s+(sha256:[0-9a-f]{64})\\s*
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
) {
        $Digest = $Matches[1]
        break
    }
}
if ($null -eq $Digest) { throw "Cannot resolve the BF16 image digest." }
$Repository = ([string]$Qwen.image) -replace ':[^/:]+
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
, ''
$PinnedImage = "$Repository@$Digest"
$GroupStatus = (& $WorkerManager -Service qwen_image_21 -Action Status -EnvFile $EnvFile -NonInteractive:$NonInteractive 6>&1 | Out-String)
Write-Host $GroupStatus.Trim()
if ($GroupStatus -notmatch [regex]::Escape("image=$PinnedImage") -or
    $GroupStatus -notmatch 'status=stopped replicas=0 pending=False' -or
    $GroupStatus -notmatch 'transport=postgres legacy_queue=False') {
    throw "Qwen is not deployed with the expected BF16 digest, or is not stopped at zero replicas."
}

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
