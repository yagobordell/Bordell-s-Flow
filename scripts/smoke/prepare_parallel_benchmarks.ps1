[CmdletBinding()]
param([string]$EnvFile = ".env")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
$Manager = Join-Path $RepoRoot "scripts/salad/manage_salad_worker.ps1"
. (Join-Path $RepoRoot "scripts/salad/_env_file.ps1")
. (Join-Path $PSScriptRoot "_salad_prepared_benchmark_worker.ps1")
Import-EnvFile -Path $EnvFile

foreach ($Name in @("docker", "uv", "ffprobe")) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required benchmark executable not found: $Name"
    }
}
& docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running." }

$Manifest = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy/salad/services.json") -Raw |
    ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($env:SALAD_API_KEY)) {
    throw "SALAD_API_KEY is missing. Add it to .env."
}
if (
    $env:SALAD_ORGANIZATION -ne [string]$Manifest.stack.organization -or
    $env:SALAD_PROJECT -ne [string]$Manifest.stack.project
) {
    throw "Salad organization/project differs from deploy/salad/services.json."
}
$BaseUrl = "https://api.salad.com/api/public/organizations/$($Manifest.stack.organization)/" +
    "projects/$($Manifest.stack.project)/containers"
$Headers = @{ "Salad-Api-Key" = $env:SALAD_API_KEY; "Accept" = "application/json" }
$Services = @("qwen_image_21", "ltx25")
$Groups = @{}

function Get-BenchmarkGroup {
    param([Parameter(Mandatory)][string]$Name)
    $Definition = $Manifest.services.$Name
    try {
        return Invoke-RestMethod -Method Get -Uri "$BaseUrl/$($Definition.group_name)" -Headers $Headers -TimeoutSec 30
    }
    catch {
        $Response = $_.Exception.Response
        if ($null -ne $Response -and [int]$Response.StatusCode -eq 404) {
            return $null
        }
        throw
    }
}

# Check both groups before modifying either: never interrupt running workers
# or a group whose deployment update is still pending.
foreach ($Name in $Services) {
    $Group = Get-BenchmarkGroup -Name $Name
    if ($null -ne $Group -and (
        [string]$Group.current_state.status -ne "stopped" -or
        [bool]$Group.pending_change
    )) {
        throw "$Name must be stopped/pending=False before parallel preparation."
    }
    $Groups[$Name] = $Group
}

# Salad may leave a stopped group with a residual desired replica after an
# image upgrade. Recover via the normal Stop lifecycle; never rebuild an image
# or mutate a group that is running/pending. The manager waits for true zero.
foreach ($Name in $Services) {
    $Group = $Groups[$Name]
    if ($null -ne $Group -and [int]$Group.replicas -ne 0) {
        Write-Warning (
            "$Name is stopped with replicas=$([int]$Group.replicas); " +
            "normalizing before any benchmark preparation."
        )
        & $Manager -Service $Name -Action Stop -EnvFile $EnvFile -NonInteractive
        if (-not $?) { throw "$Name zero-replica cleanup failed." }
        $Group = Get-BenchmarkGroup -Name $Name
        if (
            $null -eq $Group -or
            [string]$Group.current_state.status -ne "stopped" -or
            [int]$Group.replicas -ne 0 -or
            [bool]$Group.pending_change
        ) {
            throw "$Name did not reach stopped/replicas=0/pending=False after cleanup."
        }
        $Groups[$Name] = $Group
    }
}

foreach ($Name in $Services) {
    $Definition = $Manifest.services.$Name
    $Group = $Groups[$Name]
    $Pinned = $null
    try {
        $Pinned = Resolve-SaladBenchmarkPinnedImage -Definition $Definition
    }
    catch {
        Write-Host "$Name published tag not available: $($Definition.image)" -ForegroundColor Yellow
    }

    if (
        $null -ne $Group -and
        $null -ne $Pinned -and
        [string]$Group.container.image -eq $Pinned -and
        [string]$Group.priority -eq [string]$Definition.priority -and
        [string]$Group.queue_connection.queue_name -eq [string]$Definition.queue_name
    ) {
        Write-Host "$Name already prepared at $Pinned; skipping Docker build and Salad upgrade."
        continue
    }

    $Options = @{ Service = $Name; Action = "Prepare"; EnvFile = $EnvFile; NonInteractive = $true }
    if ($null -ne $Pinned) {
        $Options["SkipBuild"] = $true
        $Options["PinnedImage"] = $Pinned
        Write-Host "$Name reusing published immutable image $Pinned"
    }
    else {
        Write-Host "$Name building and publishing $($Definition.image)"
    }
    & $Manager @Options
    if (-not $?) { throw "$Name Prepare failed; neither benchmark has been started." }
    $Prepared = Get-BenchmarkGroup -Name $Name
    Assert-SaladPreparedBenchmarkWorker -Group $Prepared -Definition $Definition
}

Write-Host "Both benchmark workers are prepared at zero replicas." -ForegroundColor Green
Write-Host "Run the two benchmark scripts in separate PowerShell windows with -UsePreparedImage."
