[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PromptFile,
    [ValidateRange(0, 2147483647)][int]$Seed = 4242,
    [string]$EnvFile = ".env",
    [string]$OutputDir = "data/output/deployment-validation/qwen-image-21-benchmark",
    [switch]$UsePreparedImage,
    [ValidateRange(1, 60)][int]$AllocatingTimeoutMinutes = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
$Manager = Join-Path $RepoRoot "scripts/salad/manage_salad_worker.ps1"
$Bootstrap = Join-Path $RepoRoot "scripts/salad/start_salad_protected_smoke.ps1"
$Submit = Join-Path $RepoRoot "scripts/smoke/submit_qwen_image_21_benchmark.py"

function Resolve-LocalPath {
    param([Parameter(Mandatory)][string]$Path)
    if ([IO.Path]::IsPathRooted($Path)) {
        return [IO.Path]::GetFullPath($Path)
    }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $Path))
}

$PromptFile = Resolve-LocalPath $PromptFile
$EnvFile = Resolve-LocalPath $EnvFile
$OutputDir = Resolve-LocalPath $OutputDir
foreach ($Path in @($PromptFile, $Manager, $Bootstrap, $Submit)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required Qwen benchmark file not found: $Path"
    }
}
if ([string]::IsNullOrWhiteSpace([IO.File]::ReadAllText($PromptFile))) {
    throw "Qwen benchmark prompt file must contain the exact historical prompt."
}
foreach ($Command in @("docker", "uv")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "Required Qwen benchmark executable not found: $Command"
    }
}
& docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is unavailable. Start Docker Desktop before allocating a Qwen worker."
}

. (Join-Path $RepoRoot "scripts/salad/_env_file.ps1")
Import-EnvFile -Path $EnvFile
$Manifest = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy/salad/services.json") -Raw | ConvertFrom-Json
$Definition = $Manifest.services.qwen_image_21
$RequiredEnv = @(
    "SALAD_API_KEY", "SALAD_ORGANIZATION", "SALAD_PROJECT",
    "R2_ENDPOINT_URL", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"
) + @($Manifest.stack.shared_required_environment) + @($Definition.required_environment)
$Missing = @($RequiredEnv | Select-Object -Unique | Where-Object {
    [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable([string]$_))
})
if ($Missing.Count -gt 0) {
    throw "Missing Qwen benchmark environment variables: $($Missing -join ', ')"
}
if (
    $env:SALAD_ORGANIZATION -ne [string]$Manifest.stack.organization -or
    $env:SALAD_PROJECT -ne [string]$Manifest.stack.project
) {
    throw "Salad organization/project in .env do not match deploy/salad/services.json."
}
if ([string]$Definition.environment.QWEN_IMAGE_21_MEMORY_MODE -ne "int8_cuda") {
    throw "Qwen benchmark requires QWEN_IMAGE_21_MEMORY_MODE=int8_cuda."
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".venv") -PathType Container)) {
    throw "Project virtual environment is missing. Run uv sync --locked --extra dev."
}

# Refuse to stop or reconfigure a worker owned by an unrelated workflow.
$GroupUrl = "https://api.salad.com/api/public/organizations/$($Manifest.stack.organization)/" +
    "projects/$($Manifest.stack.project)/containers/$($Definition.group_name)"
$Group = Invoke-RestMethod -Method Get -Uri $GroupUrl -Headers @{
    "Salad-Api-Key" = $env:SALAD_API_KEY
    "Accept" = "application/json"
} -TimeoutSec 30
if (
    [string]$Group.current_state.status -ne "stopped" -or
    [int]$Group.replicas -ne 0 -or
    [bool]$Group.pending_change
) {
    throw "Qwen worker is not stopped/zero-replica; refusing to interrupt existing work."
}

$BatchId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ") + "-" +
    [guid]::NewGuid().ToString("N").Substring(0, 8)
$BatchDir = Join-Path $OutputDir $BatchId
New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null
$SummaryFile = Join-Path $BatchDir "benchmark-summary.json"
$Common = @{ Service = "qwen_image_21"; EnvFile = $EnvFile; NonInteractive = $true }
$Failure = $null
$StopError = $null
$Stopped = $false
$OwnsWorker = $false

try {
    $OwnsWorker = $true
    if ($UsePreparedImage) {
        . (Join-Path $PSScriptRoot "_salad_prepared_benchmark_worker.ps1")
        Assert-SaladPreparedBenchmarkWorker -Group $Group -Definition $Definition
    }
    else {
        Write-Host "Building, publishing and applying Qwen image: $($Definition.image)"
        & $Manager @Common -Action Prepare
        if (-not $?) { throw "Qwen Prepare failed." }
    }

    Write-Host "Bootstrapping one protected RTX 5090 for all five generations."
    & $Bootstrap @Common -TimeoutMinutes 60 -RunningNotReadyTimeoutMinutes 60 -AllocatingTimeoutMinutes $AllocatingTimeoutMinutes
    if (-not $?) { throw "Qwen protected bootstrap failed." }

    Set-Location $RepoRoot
    & uv run --no-sync python $Submit --prompt-file $PromptFile --seed $Seed --output-dir $BatchDir
    if ($LASTEXITCODE -ne 0) {
        throw "Qwen five-run benchmark failed; inspect its summary and container logs."
    }
}
catch {
    $Failure = $_
}
finally {
    if ($OwnsWorker) {
        try {
            Write-Host "Stopping Qwen and restoring zero replicas."
            & $Manager @Common -Action Stop
            if (-not $?) { throw "Qwen worker stop failed." }
            $Stopped = $true
        }
        catch {
            $StopError = $_
            Write-Warning "Qwen worker cleanup requires attention: $($_.Exception.Message)"
        }
    }

    if (Test-Path -LiteralPath $SummaryFile -PathType Leaf) {
        $Summary = Get-Content -LiteralPath $SummaryFile -Raw | ConvertFrom-Json
    }
    else {
        $Summary = [pscustomobject]@{ runs = @(); run_error = $null }
    }
    $Summary | Add-Member -NotePropertyName worker_stopped -NotePropertyValue $Stopped -Force
    $Summary | Add-Member -NotePropertyName cleanup_error -NotePropertyValue $(
        if ($null -ne $StopError) { $StopError.Exception.Message } else { $null }
    ) -Force
    if ($null -ne $Failure) {
        $Summary | Add-Member -NotePropertyName run_error -NotePropertyValue $Failure.Exception.Message -Force
    }
    $Summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $SummaryFile -Encoding utf8
    Write-Host "Qwen benchmark report: $SummaryFile"
}
if ($null -ne $Failure) { throw $Failure }
if ($null -ne $StopError) { throw $StopError }
Write-Host "Qwen benchmark completed. Review the PNGs and warm-run inference median."
