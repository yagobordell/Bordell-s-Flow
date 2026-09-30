[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^ghcr\.io/yagobordell/image-upscaler-salad@sha256:[0-9a-f]{64}$')]
    [string]$Image,
    [string]$EnvFile = ".env",
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\image-upscaler-service.json"
. (Join-Path $PSScriptRoot "_env_file.ps1")
Import-EnvFile -Path $EnvFile

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$Service = $Manifest.service
if ([int]$Manifest.schema_version -ne 1) { throw "Image upscaler manifest must use schema_version=1." }
if ([int]$Service.capacity.replicas -ne 1) { throw "Image upscaler must start with one replica." }
if ([int]$Service.resources.cpu -ne 4) { throw "Image upscaler CPU must remain 4." }
if ([int]$Service.resources.memory -ne 16384) { throw "Image upscaler RAM must remain 16 GiB." }
if ([int]$Service.networking.port -ne 8080 -or -not [bool]$Service.networking.auth) {
    throw "Image upscaler gateway must be authenticated HTTP on port 8080."
}
if ($ValidateOnly) { Write-Host "Image upscaler Salad manifest validation OK."; exit 0 }

function Get-RequiredSetting {
    param([Parameter(Mandatory)][string]$Name)
    $Value = [Environment]::GetEnvironmentVariable($Name, [EnvironmentVariableTarget]::Process)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "Missing required setting: $Name" }
    return $Value.Trim()
}

$Organization = Get-RequiredSetting "SALAD_ORGANIZATION"
$Project = Get-RequiredSetting "SALAD_PROJECT"
$ApiKey = Get-RequiredSetting "SALAD_API_KEY"
$GhcrPat = Get-RequiredSetting "GHCR_READ_PAT"
$Headers = @{ "Salad-Api-Key" = $ApiKey; "Accept" = "application/json"; "User-Agent" = "bordell-image-upscaler-deployer/1.0" }
$OrganizationBase = "https://api.salad.com/api/public/organizations/$Organization"
$ContainersBase = "$OrganizationBase/projects/$Project/containers"

$GpuResponse = Invoke-RestMethod -Method Get -Uri "$OrganizationBase/gpu-classes" -Headers $Headers
$GpuItems = if ($GpuResponse.PSObject.Properties.Name -contains "items") { @($GpuResponse.items) } else { @($GpuResponse) }
$GpuName = [string]$Service.resources.gpu_class_names[0]
$Matches = @($GpuItems | Where-Object { [string]$_.name -eq $GpuName })
if ($Matches.Count -ne 1) { throw "GPU class '$GpuName' did not resolve exactly once." }
$GpuClassId = [string]$Matches[0].id

function New-Probe {
    param([Parameter(Mandatory)][object]$Probe)
    return @{
        http = @{ headers = @(); path = [string]$Probe.path; port = [int]$Probe.port; scheme = "http" }
        initial_delay_seconds = 0
        period_seconds = [int]$Probe.period_seconds
        failure_threshold = [int]$Probe.failure_threshold
        success_threshold = 1
        timeout_seconds = [int]$Probe.timeout_seconds
    }
}

$Body = @{
    name = [string]$Service.group_name
    display_name = [string]$Service.display_name
    replicas = 1
    restart_policy = "always"
    autostart_policy = $true
    scheduled_scaling_enabled = $false
    networking = @{ protocol = "http"; port = 8080; auth = $true }
    container = @{
        image = $Image
        image_caching = $true
        priority = "high"
        resources = @{ cpu = 4; memory = 16384; gpu_classes = @($GpuClassId) }
        registry_authentication = @{ basic = @{ username = "yagobordell"; password = $GhcrPat } }
    }
    startup_probe = New-Probe $Service.probes.startup
    readiness_probe = New-Probe $Service.probes.readiness
    liveness_probe = New-Probe $Service.probes.liveness
} | ConvertTo-Json -Depth 20

$GroupName = [string]$Service.group_name
try {
    Invoke-RestMethod -Method Get -Uri "$ContainersBase/$GroupName" -Headers $Headers | Out-Null
    throw "Container group '$GroupName' already exists; refusing to replace it implicitly."
}
catch {
    $Status = $null
    if ($null -ne $_.Exception.Response) { $Status = [int]$_.Exception.Response.StatusCode }
    if ($Status -ne 404) { throw }
}

Invoke-RestMethod -Method Post -Uri $ContainersBase -Headers $Headers -ContentType "application/json" -Body $Body | Out-Null
$Created = Invoke-RestMethod -Method Get -Uri "$ContainersBase/$GroupName" -Headers $Headers
if ([string]$Created.container.image -ne $Image) { throw "Salad did not retain the requested image digest." }
if ([int]$Created.replicas -ne 1) { throw "Salad did not retain one replica." }
if (-not [bool]$Created.networking.auth -or [int]$Created.networking.port -ne 8080) {
    throw "Salad did not retain authenticated gateway networking on port 8080."
}
Write-Host "Created Salad image upscaler container group '$GroupName'."
Write-Host "Gateway DNS: $([string]$Created.networking.dns)"
