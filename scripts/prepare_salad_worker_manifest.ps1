[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "ideogram4", "flux2_klein", "ltx25")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [switch]$SkipBuild,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$WorkerManager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad service manifest not found: $ManifestPath"
}
if (-not (Test-Path -LiteralPath $WorkerManager -PathType Leaf)) {
    throw "Salad worker manager not found: $WorkerManager"
}

$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$ServiceProperty = $Document.services.PSObject.Properties[$Service]
if ($null -eq $ServiceProperty) {
    throw "Unknown Salad service '$Service'."
}
$Definition = $ServiceProperty.Value

$Previous = @{}
foreach ($Property in $Definition.environment.PSObject.Properties) {
    $Name = [string]$Property.Name
    $Previous[$Name] = [Environment]::GetEnvironmentVariable(
        $Name,
        [EnvironmentVariableTarget]::Process
    )
    [Environment]::SetEnvironmentVariable(
        $Name,
        [string]$Property.Value,
        [EnvironmentVariableTarget]::Process
    )
}

try {
    $Arguments = @{
        Action = "Prepare"
        Service = $Service
        EnvFile = $EnvFile
        PrepareTimeoutMinutes = $PrepareTimeoutMinutes
    }
    if ($SkipBuild) {
        $Arguments["SkipBuild"] = $true
    }
    if ($NonInteractive) {
        $Arguments["NonInteractive"] = $true
    }

    Write-Host (
        "Preparing {0} with manifest-authoritative runtime environment." -f $Service
    ) -ForegroundColor Cyan
    & $WorkerManager @Arguments
    if (-not $?) {
        throw "Salad worker Prepare failed for service '$Service'."
    }
}
finally {
    foreach ($Entry in $Previous.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable(
            [string]$Entry.Key,
            $Entry.Value,
            [EnvironmentVariableTarget]::Process
        )
    }
}
