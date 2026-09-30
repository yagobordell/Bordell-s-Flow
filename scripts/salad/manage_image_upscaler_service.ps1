[CmdletBinding()]
param(
    [ValidateSet("Validate", "Publish", "Deploy", "Status")]
    [string]$Action = "Status",
    [string]$PinnedImage = "",
    [string]$EnvFile = ".env",
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\image-upscaler-service.json"
. (Join-Path $PSScriptRoot "_env_file.ps1")
. (Join-Path $PSScriptRoot "_http_status.ps1")
Import-EnvFile -Path $EnvFile

$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ([int]$Document.schema_version -ne 1) { throw "Image upscaler manifest must use schema_version=1." }
$Definition = $Document.service
$GroupName = [string]$Definition.group_name
$Dockerfile = Join-Path $RepoRoot ([string]$Definition.dockerfile)
$MutableImage = [string]$Definition.image

function Get-Setting {
    param([string]$Name, [string]$Prompt, [switch]$Secret)
    $Value = [Environment]::GetEnvironmentVariable($Name, [EnvironmentVariableTarget]::Process)
    if (-not [string]::IsNullOrWhiteSpace($Value)) { return $Value.Trim() }
    if ($NonInteractive) { throw "$Name is missing and -NonInteractive was requested." }
    if ($Secret) {
        $Secure = Read-Host $Prompt -AsSecureString
        $Value = [PSCredential]::new("image-upscaler", $Secure).GetNetworkCredential().Password
    }
    else { $Value = Read-Host $Prompt }
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Name is empty." }
    return $Value.Trim()
}

function Get-Headers {
    return @{
        "Salad-Api-Key" = Get-Setting -Name "SALAD_API_KEY" -Prompt "Salad API key" -Secret
        "Accept" = "application/json"
        "User-Agent" = "bordell-image-upscaler-manager/1.0"
    }
}

function Invoke-Salad {
    param([hashtable]$Headers, [string]$Uri, [string]$Method = "Get", [object]$Body = $null)
    $Request = @{ Method = $Method; Uri = $Uri; Headers = $Headers; TimeoutSec = 60 }
    if ($null -ne $Body) {
        $Request["ContentType"] = "application/json"
        $Request["Body"] = $Body
    }
    return Invoke-RestMethod @Request
}

function Resolve-PinnedImage {
    $Inspect = & docker buildx imagetools inspect $MutableImage 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect published image: $MutableImage" }
    $Digest = $null
    foreach ($Line in $Inspect) {
        if ([string]$Line -match '^\s*Digest:\s+(sha256:[0-9a-f]{64})\s*$') {
            $Digest = $Matches[1]
            break
        }
    }
    if ($null -eq $Digest) { throw "Could not resolve the image digest." }
    if ($MutableImage -notmatch '^(?<repo>.+):[^/:]+$') { throw "Image tag is invalid." }
    return "$([string]$Matches['repo'])@$Digest"
}

function New-Probe {
    param([object]$Probe)
    return @{
        http = @{
            headers = @()
            path = [string]$Probe.path
            port = [int]$Probe.port
            scheme = "http"
        }
        initial_delay_seconds = 0
        period_seconds = [int]$Probe.period_seconds
        timeout_seconds = [int]$Probe.timeout_seconds
        success_threshold = 1
        failure_threshold = [int]$Probe.failure_threshold
    }
}

$ExpectedSource = (
    "ghcr.io/yagobordell/image-upscaler@" +
    "sha256:00e046a7ba6dd00d406609f2f5c007b176b96578b1baac612ea8e97940c35aba"
)
if ([string]$Definition.source_image -ne $ExpectedSource) { throw "Unexpected OptiumLabs source digest." }
if (-not (Test-Path -LiteralPath $Dockerfile -PathType Leaf)) { throw "Missing Dockerfile: $Dockerfile" }
if ([int]$Definition.capacity.replicas -ne 1) { throw "Image upscaler must use one replica." }
if ([int]$Definition.resources.cpu -ne 4 -or [int]$Definition.resources.memory -ne 16384) {
    throw "Image upscaler must use 4 CPU / 16 GiB RAM."
}
if ([string]$Definition.resources.gpu_class_names[0] -ne "RTX 5090 (32 GB)") {
    throw "Image upscaler must use RTX 5090 (32 GB)."
}
if ([int]$Definition.networking.port -ne 8080 -or -not [bool]$Definition.networking.auth) {
    throw "Container Gateway must use authenticated port 8080."
}

Set-Location $RepoRoot
if ($Action -eq "Validate") {
    Write-Host "VALID image-upscaler Salad adapter and manifest." -ForegroundColor Green
    exit 0
}

if ($Action -eq "Publish") {
    & docker buildx build --platform linux/amd64 --file $Dockerfile --tag $MutableImage --push .
    if ($LASTEXITCODE -ne 0) { throw "Image build or GHCR push failed." }
    Write-Host "Published: $(Resolve-PinnedImage)" -ForegroundColor Green
    exit 0
}

$Organization = Get-Setting -Name "SALAD_ORGANIZATION" -Prompt "Salad organization"
$Project = Get-Setting -Name "SALAD_PROJECT" -Prompt "Salad project"
$Headers = Get-Headers
$OrganizationBase = "https://api.salad.com/api/public/organizations/$Organization"
$ContainersBase = "$OrganizationBase/projects/$Project/containers"
$GroupUrl = "$ContainersBase/$GroupName"

if ($Action -eq "Status") {
    try { $Group = Invoke-Salad -Headers $Headers -Uri $GroupUrl }
    catch {
        if ((Get-HttpStatusCode -ErrorRecord $_) -eq 404) {
            Write-Output "group=$GroupName status=missing"
            exit 0
        }
        throw
    }
    Write-Output "group=$GroupName replicas=$($Group.replicas) pending=$($Group.pending_change)"
    if ($null -ne $Group.networking -and $null -ne $Group.networking.dns) {
        Write-Output "IMAGE_UPSCALER_URL=https://$($Group.networking.dns)"
    }
    exit 0
}

if ($Action -ne "Deploy") { throw "Unsupported action '$Action'." }
$RegistryPat = Get-Setting -Name "GHCR_READ_PACKAGES_PAT" -Prompt "GHCR PAT (read:packages)" -Secret
if ([string]::IsNullOrWhiteSpace($PinnedImage)) { $PinnedImage = Resolve-PinnedImage }
if ($PinnedImage -notmatch '^ghcr\.io/yagobordell/image-upscaler-salad@sha256:[0-9a-f]{64}$') {
    throw "Deploy requires an immutable image-upscaler-salad GHCR digest."
}

$GpuResponse = Invoke-Salad -Headers $Headers -Uri "$OrganizationBase/gpu-classes"
$GpuItems = if ($GpuResponse.PSObject.Properties.Name -contains "items") { @($GpuResponse.items) } else { @($GpuResponse) }
$GpuName = [string]$Definition.resources.gpu_class_names[0]
$GpuMatches = @($GpuItems | Where-Object { [string]$_.name -eq $GpuName })
if ($GpuMatches.Count -ne 1) { throw "GPU class '$GpuName' did not resolve exactly once." }

$Deployment = @{
    display_name = [string]$Definition.display_name
    autostart_policy = $true
    replicas = 1
    restart_policy = "always"
    container = @{
        image = $PinnedImage
        resources = @{
            cpu = 4
            memory = 16384
            gpu_classes = @([string]$GpuMatches[0].id)
        }
        registry_authentication = @{
            basic = @{
                username = "yagobordell"
                password = $RegistryPat
            }
        }
        image_caching = $true
    }
    networking = @{
        protocol = "http"
        port = 8080
        auth = $true
    }
    startup_probe = New-Probe -Probe $Definition.probes.startup
    readiness_probe = New-Probe -Probe $Definition.probes.readiness
    liveness_probe = New-Probe -Probe $Definition.probes.liveness
}

$Existing = $null
try { $Existing = Invoke-Salad -Headers $Headers -Uri $GroupUrl }
catch { if ((Get-HttpStatusCode -ErrorRecord $_) -ne 404) { throw } }

if ($null -eq $Existing) {
    $Deployment["name"] = $GroupName
    Invoke-Salad -Headers $Headers -Uri $ContainersBase -Method "Post" -Body ($Deployment | ConvertTo-Json -Depth 20) | Out-Null
}
else {
    Invoke-Salad -Headers $Headers -Uri $GroupUrl -Method "Patch" -Body ($Deployment | ConvertTo-Json -Depth 20) | Out-Null
}

Write-Host "Image upscaler deployment submitted with one RTX 5090 replica." -ForegroundColor Green
Write-Host "Run this script with -Action Status to obtain IMAGE_UPSCALER_URL."
