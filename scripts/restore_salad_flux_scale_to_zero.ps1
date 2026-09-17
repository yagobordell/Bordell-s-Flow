[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [ValidateRange(15, 300)]
    [int]$TimeoutSeconds = 180,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$Service = "flux_schnell"

function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path $RepoRoot $Resolved
    }
    if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
        return
    }
    foreach ($RawLine in Get-Content -LiteralPath $Resolved) {
        $Line = $RawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($Line) -or $Line.StartsWith("#")) {
            continue
        }
        if ($Line -notmatch '^(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$') {
            continue
        }
        $Name = $Matches["name"]
        $Value = $Matches["value"].Trim().Trim('"').Trim("'")
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name))) {
            [Environment]::SetEnvironmentVariable($Name, $Value)
        }
    }
}

function Get-SaladApiKey {
    $Value = [Environment]::GetEnvironmentVariable("SALAD_API_KEY")
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value.Trim()
    }
    if ($NonInteractive) {
        throw "SALAD_API_KEY is missing and -NonInteractive was requested."
    }
    $SecureValue = Read-Host "Salad API key" -AsSecureString
    $Credential = [PSCredential]::new("salad-flux-restore", $SecureValue)
    $Value = $Credential.GetNetworkCredential().Password
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "SALAD_API_KEY is empty."
    }
    return $Value.Trim()
}

Import-EnvFile -Path $EnvFile
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad stack manifest not found: $ManifestPath"
}
$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$Definition = $Document.services.$Service
if ($null -eq $Definition) {
    throw "Manifest service '$Service' is missing."
}

$Organization = [string]$Document.stack.organization
$Project = [string]$Document.stack.project
$GroupName = [string]$Definition.group_name
$BaseUrl = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$GroupUrl = "$BaseUrl/containers/$GroupName"
$Headers = @{
    "Salad-Api-Key" = Get-SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-flux-restore/1.0"
}

function Get-Group {
    return Invoke-RestMethod -Uri $GroupUrl -Headers $Headers -TimeoutSec 30
}

$Autoscaler = @{
    min_replicas = [int]$Definition.autoscaler.min_replicas
    max_replicas = [int]$Definition.autoscaler.max_replicas
    desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
    polling_period = [int]$Definition.autoscaler.polling_period
    max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
    max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
}

$Group = Get-Group
if ([int]$Group.queue_autoscaler.min_replicas -ne [int]$Definition.autoscaler.min_replicas) {
    Write-Host "FLUX restore: returning queue autoscaler to manifest min_replicas=0." -ForegroundColor Cyan
    Invoke-RestMethod `
        -Method Patch `
        -Uri $GroupUrl `
        -Headers $Headers `
        -ContentType "application/merge-patch+json" `
        -Body (@{ queue_autoscaler = $Autoscaler } | ConvertTo-Json -Depth 10) `
        -TimeoutSec 60 |
        Out-Null
}

$Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Seconds 3
    $Group = Get-Group
    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.queue_autoscaler.min_replicas -eq [int]$Definition.autoscaler.min_replicas
    ) {
        break
    }
}
while ((Get-Date) -lt $Deadline)
if (
    [bool]$Group.pending_change -or
    [int]$Group.queue_autoscaler.min_replicas -ne [int]$Definition.autoscaler.min_replicas
) {
    throw "FLUX autoscaler did not return to manifest scale-to-zero settings before timeout."
}

$Status = [string]$Group.current_state.status
if ($Status -ne "stopped") {
    Write-Host "FLUX restore: stopping fallback worker group." -ForegroundColor Cyan
    Invoke-RestMethod `
        -Method Post `
        -Uri "$GroupUrl/stop" `
        -Headers $Headers `
        -TimeoutSec 60 |
        Out-Null
}

$StopDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Seconds 3
    $Group = Get-Group
    $Status = [string]$Group.current_state.status
    if ($Status -eq "stopped" -and -not [bool]$Group.pending_change -and [int]$Group.replicas -eq 0) {
        Write-Host "FLUX fallback restored: stopped at zero replicas with manifest scale-to-zero." -ForegroundColor Green
        exit 0
    }
}
while ((Get-Date) -lt $StopDeadline)

throw (
    "FLUX fallback did not stop cleanly before timeout; " +
    "status=$Status replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
)
