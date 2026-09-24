[CmdletBinding()]
param(
    [ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")]
    [string]$Action = "Status",
    [string[]]$Services = @(),
    [string]$EnvFile = ".env",
    [string]$PinnedImage = "",
    [ValidateRange(10, 180)][int]$PrepareTimeoutMinutes = 120,
    [switch]$SkipBuild,
    [switch]$Recreate,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$WorkerManager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { throw "Missing Salad manifest: $ManifestPath" }
if (-not (Test-Path -LiteralPath $WorkerManager -PathType Leaf)) { throw "Missing worker manager: $WorkerManager" }

$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ([int]$Document.schema_version -ne 3) { throw "deploy/salad/services.json must use schema_version=3." }
if ([string]$Document.stack.job_transport -ne "postgres") { throw "Salad stack job_transport must be postgres." }

$ConfiguredOrder = @($Document.stack.service_order | ForEach-Object { [string]$_ })
if ($ConfiguredOrder.Count -eq 0 -or $ConfiguredOrder.Count -ne @($ConfiguredOrder | Select-Object -Unique).Count) {
    throw "stack.service_order must contain unique service names."
}
$Known = @($Document.services.PSObject.Properties.Name)
foreach ($Name in $ConfiguredOrder) {
    if ($Known -notcontains $Name) { throw "service_order references unknown service '$Name'." }
}

if ($Services.Count -eq 0) {
    $Selected = @($ConfiguredOrder)
}
else {
    $Selected = @()
    foreach ($Name in $Services) {
        if ($ConfiguredOrder -notcontains $Name) { throw "Unknown Salad service '$Name'." }
        if ($Selected -notcontains $Name) { $Selected += $Name }
    }
}

if (-not [string]::IsNullOrWhiteSpace($PinnedImage) -and $Selected.Count -ne 1) {
    throw "-PinnedImage requires exactly one selected service."
}
if ($Recreate -and $Action -ne "Prepare") { throw "-Recreate is only valid with Prepare." }
if ($Recreate -and $Selected.Count -ne 1) { throw "-Recreate requires exactly one selected service." }

$ExecutionOrder = @($Selected)
if ($Action -eq "Stop") { [array]::Reverse($ExecutionOrder) }

foreach ($Name in $ExecutionOrder) {
    Write-Host "=== Salad $Action : $Name ===" -ForegroundColor Cyan
    $Arguments = @{
        Action = $Action
        Service = $Name
        EnvFile = $EnvFile
        PrepareTimeoutMinutes = $PrepareTimeoutMinutes
    }
    if ($SkipBuild) { $Arguments["SkipBuild"] = $true }
    if ($Recreate) { $Arguments["Recreate"] = $true }
    if ($NonInteractive) { $Arguments["NonInteractive"] = $true }
    if (-not [string]::IsNullOrWhiteSpace($PinnedImage)) { $Arguments["PinnedImage"] = $PinnedImage }

    & $WorkerManager @Arguments
    if (-not $?) { throw "Salad $Action failed for service '$Name'." }
}

Write-Host ("Salad stack action complete: action={0} services={1}" -f $Action, ($ExecutionOrder -join ",")) -ForegroundColor Green
