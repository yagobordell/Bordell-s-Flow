# Controlled rollout of the existing LTX telemetry migration + shared worker image.
# Dry-run by default. Requires local Postgres/Salad/Docker credentials for -Apply.
[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$EnvFile = ".env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$WorkerManager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$Migration = Join-Path $PSScriptRoot "apply_ltx_telemetry_migration.py"
$Services = @("ltx25_i2v", "ltx25")

function Assert-Success {
    param([Parameter(Mandatory)][string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed (exit code $LASTEXITCODE)."
    }
}

function Get-StoppedStatus {
    param([Parameter(Mandatory)][string]$Service)
    $Result = @(& $WorkerManager -Action Status -Service $Service -EnvFile $EnvFile -NonInteractive)
    if (-not $?) { throw "Failed to obtain Salad status for $Service" }
    $Status = ($Result -join " ").Trim()
    if ($Status -notmatch '(?:^|\s)status=stopped(?:\s|$)' -or
        $Status -notmatch '(?:^|\s)pending=[Ff]alse(?:\s|$)') {
        throw "Refusing to update ${Service}: it must be stably stopped (status: $Status). " +
              "Safely stop the singleton controller and drain in-flight work first; " +
              "never bypass its advisory lock."
    }
    return $Status
}

Push-Location $RepoRoot
try {
    $Branch = (& git branch --show-current).Trim()
    Assert-Success "git branch"
    if ($Branch -ne "feature/ltx-time-aware-autoscaling") {
        throw "Refusing rollout outside feature/ltx-time-aware-autoscaling (found: $Branch)."
    }
    $Changes = @(& git status --porcelain --untracked-files=no)
    Assert-Success "git status"
    if ($Changes.Count -gt 0 -and ($Changes -join "").Trim()) {
        throw "Tracked working-tree changes detected; refusing an unreviewed Docker build."
    }
    $Commit = (& git rev-parse --short=12 HEAD).Trim()
    Assert-Success "git rev-parse"

    $Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ([string]$Document.services.ltx25.dockerfile -ne
        [string]$Document.services.ltx25_i2v.dockerfile) {
        throw "LTX I2V and A2V Dockerfile paths differ; refusing shared image rollout."
    }
    $ReferenceImage = [string]$Document.services.ltx25.image
    if ($ReferenceImage -ne [string]$Document.services.ltx25_i2v.image) {
        throw "LTX services do not share the same reference image; refusing deployment."
    }
    if ($ReferenceImage -notmatch '^(?<repository>.+):[^/:]+$') {
        throw "Invalid LTX Docker image reference in manifest."
    }
    $Repository = $Matches["repository"]
    $Tag = "${Repository}:ltx25-telemetry-${Commit}"

    foreach ($Service in $Services) {
        & $WorkerManager -Action Validate -Service $Service -EnvFile $EnvFile -NonInteractive
        if (-not $?) { throw "Offline Salad validation failed for $Service." }
    }
    if (-not $Apply) {
        Write-Host "DRY RUN: no Postgres, Salad, or Docker mutations." -ForegroundColor Cyan
        Write-Host "Proposed image: $Tag"
        Write-Host "On -Apply: require both groups stopped; apply SQL 009; build once; prepare both groups with the pinned digest."
        Write-Host "No worker will be started and no paid GPU generation will be launched."
        exit 0
    }

    # Avoid doing even the database migration while either provider group is live.
    foreach ($Service in $Services) {
        Get-StoppedStatus -Service $Service | Write-Host
    }

    # Probe Docker before touching the database to avoid a partial rollout
    # when Docker Desktop is not running or the engine is unavailable.
    # Do not suppress Docker's diagnostic output.
    & docker version
    Assert-Success "Docker availability (start Docker Desktop and use Linux containers)"
    $DockerOS = @(& docker info --format '{{.OSType}}')
    Assert-Success "Docker daemon status"
    if (($DockerOS -join "").Trim() -ne "linux") {
        throw "LTX requires a Linux Docker engine, got: $($DockerOS -join ', '). Switch Docker Desktop to Linux containers."
    }
    & docker buildx version
    Assert-Success "Docker Buildx availability"

    & uv run --locked --extra dev python $Migration --apply
    Assert-Success "Telemetry Postgres migration"

    $Dockerfile = Join-Path $RepoRoot ([string]$Document.services.ltx25.dockerfile)
    & docker buildx build --platform linux/amd64 --file $Dockerfile --tag $Tag --push .
    Assert-Success "LTX image build/push"
    $Inspect = @(& docker buildx imagetools inspect $Tag 2>&1)
    Assert-Success "Docker digest resolution"
    $Digest = $null
    foreach ($Line in $Inspect) {
        if ([string]$Line -match '^\s*Digest:\s+(sha256:[0-9a-fA-F]{64})\s*$') {
            $Digest = $Matches[1]
            break
        }
    }
    if (-not $Digest) { throw "Could not verify the published immutable image digest." }
    $Pinned = "${Repository}@${Digest}"

    foreach ($Service in $Services) {
        # Re-check before each mutation; the manager also owns the Postgres
        # Capacity Controller advisory lock and fails closed if it is held.
        Get-StoppedStatus -Service $Service | Write-Host
        & $WorkerManager -Action Prepare -Service $Service -EnvFile $EnvFile -PinnedImage $Pinned -SkipBuild -NonInteractive
        if (-not $?) { throw "Prepare failed for $Service; do not start any GPU workers." }
        $State = Get-StoppedStatus -Service $Service
        if (-not $State.Contains("image=$Pinned")) {
            throw "Salad reports unexpected image after preparing ${Service}: $State"
        }
        Write-Host "Verified $Service stopped on immutable image $Pinned" -ForegroundColor Green
    }

    Write-Host "Rollout completed. Both groups remain stopped; no GPUs launched." -ForegroundColor Green
    Write-Host "Restart the normal singleton Capacity Controller only under the usual operator procedure."
}
finally {
    Pop-Location
}
