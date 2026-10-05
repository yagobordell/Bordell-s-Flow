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
    param(
        [hashtable]$Headers,
        [string]$Uri,
        [string]$Method = "Get",
        [object]$Body = $null,
        [string]$ContentType = ""
    )
    $Request = @{ Method = $Method; Uri = $Uri; Headers = $Headers; TimeoutSec = 60 }
    if ($null -ne $Body) {
        if ([string]::IsNullOrWhiteSpace($ContentType)) { $ContentType = "application/json" }
        $Request["ContentType"] = $ContentType
        $Request["Body"] = $Body
    }
    return Invoke-RestMethod @Request
}

function Test-InsecureRedirectError {
    param([Parameter(Mandatory)][object]$ErrorRecord)
    $Message = [string]$ErrorRecord.Exception.Message
    $Details = ""
    if ($null -ne $ErrorRecord.ErrorDetails) {
        $Details = [string]$ErrorRecord.ErrorDetails.Message
    }
    return (
        $Message -match "Cannot follow an insecure redirection" -or
        $Details -match "Cannot follow an insecure redirection"
    )
}

function Invoke-SaladMutation {
    param(
        [hashtable]$Headers,
        [string]$Uri,
        [string]$Method,
        [object]$Body,
        [string]$ContentType
    )
    try {
        Invoke-Salad -Headers $Headers -Uri $Uri -Method $Method -Body $Body -ContentType $ContentType | Out-Null
    }
    catch {
        if (-not (Test-InsecureRedirectError -ErrorRecord $_)) {
            throw
        }
        Write-Warning (
            "Salad accepted the mutation but returned an insecure redirect. " +
            "The redirect was NOT followed; the container group will be verified over HTTPS."
        )
    }
}

function Wait-SaladGroupSettled {
    param(
        [hashtable]$Headers,
        [string]$Uri,
        [int]$TimeoutSeconds = 900,
        [switch]$AllowInitialNotFound
    )
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $Seen = $false
    do {
        try {
            $Group = Invoke-Salad -Headers $Headers -Uri $Uri
            $Seen = $true
            if (-not [bool]$Group.pending_change) {
                return $Group
            }
            Write-Host "Waiting for Salad pending_change=false..."
        }
        catch {
            if (Test-InsecureRedirectError -ErrorRecord $_) {
                Write-Warning (
                    "Salad returned a transient insecure redirect while the container group " +
                    "was settling. The redirect was NOT followed; retrying the canonical " +
                    "HTTPS API endpoint."
                )
            }
            elseif (
                $AllowInitialNotFound -and
                -not $Seen -and
                (Get-HttpStatusCode -ErrorRecord $_) -eq 404
            ) {
                Write-Host "Waiting for Salad container group to become visible..."
            }
            else {
                throw
            }
        }
        Start-Sleep -Seconds 10
    } while ((Get-Date) -lt $Deadline)
    throw "Salad container group did not settle within $TimeoutSeconds seconds."
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
foreach ($ProbeName in @("startup", "readiness", "liveness")) {
    $Probe = $Definition.probes.PSObject.Properties[$ProbeName].Value
    $FailureThreshold = [int]$Probe.failure_threshold
    if ($FailureThreshold -lt 1 -or $FailureThreshold -gt 20) {
        throw "$ProbeName probe failure_threshold must be between 1 and 20 for Salad."
    }
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
    if ($null -ne $Group.container -and $null -ne $Group.container.image) {
        Write-Output "container_image=$([string]$Group.container.image)"
    }
    $CurrentStateProperty = $Group.PSObject.Properties["current_state"]
    if ($null -ne $CurrentStateProperty -and $null -ne $CurrentStateProperty.Value) {
        $CurrentState = $CurrentStateProperty.Value
        $StatusProperty = $CurrentState.PSObject.Properties["status"]
        if ($null -ne $StatusProperty) {
            Write-Output "current_state=$([string]$StatusProperty.Value)"
        }
        Write-Output "current_state_json=$($CurrentState | ConvertTo-Json -Depth 8 -Compress)"
    }
    try {
        $InstanceResponse = Invoke-Salad -Headers $Headers -Uri "$GroupUrl/instances"
        $Instances = @($InstanceResponse.instances)
        Write-Output "instances=$($Instances.Count)"
        foreach ($Instance in $Instances) {
            Write-Output "instance_json=$($Instance | ConvertTo-Json -Depth 8 -Compress)"
        }
    }
    catch {
        Write-Warning "Could not list Salad container-group instances: $($_.Exception.Message)"
    }
    if ($null -ne $Group.networking -and $null -ne $Group.networking.dns) {
        Write-Output "IMAGE_UPSCALER_URL=https://$($Group.networking.dns)"
    }
    exit 0
}

if ($Action -ne "Deploy") { throw "Unsupported action '$Action'." }
$RegistryUsername = Get-Setting -Name "GHCR_USERNAME" -Prompt "GitHub username for GHCR PAT"
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
                username = $RegistryUsername
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

if ($null -ne $Existing -and [bool]$Existing.pending_change) {
    Write-Host "Salad has a pending container-group update; waiting before deployment..."
    $Existing = Wait-SaladGroupSettled -Headers $Headers -Uri $GroupUrl
}

$DeploymentJson = $Deployment | ConvertTo-Json -Depth 20
$Created = $false
if ($null -eq $Existing) {
    $Created = $true
    $Deployment["name"] = $GroupName
    $DeploymentJson = $Deployment | ConvertTo-Json -Depth 20
    try {
        Invoke-SaladMutation -Headers $Headers -Uri $ContainersBase -Method "Post" -Body $DeploymentJson -ContentType "application/json"
    }
    catch {
        $Details = ""
        if ($null -ne $_.ErrorDetails) {
            $Details = [string]$_.ErrorDetails.Message
        }
        if (
            (Get-HttpStatusCode -ErrorRecord $_) -eq 400 -and
            $Details -match '"type"\s*:\s*"name_conflict"'
        ) {
            throw (
                "Salad reports group name '$GroupName' as reserved even though it is " +
                "not visible in project inventory. Do not retry the same hidden name; " +
                "version deploy/salad/image-upscaler-service.json to a new group_name."
            )
        }
        throw
    }
}
else {
    Invoke-SaladMutation -Headers $Headers -Uri $GroupUrl -Method "Patch" -Body $DeploymentJson -ContentType "application/merge-patch+json"
}

$Verified = if ($Created) {
    Wait-SaladGroupSettled -Headers $Headers -Uri $GroupUrl -AllowInitialNotFound
}
else {
    Wait-SaladGroupSettled -Headers $Headers -Uri $GroupUrl
}
if ($null -eq $Verified) { throw "Salad did not expose the container group after deployment." }
if ([string]$Verified.container.image -ne $PinnedImage) {
    throw "Salad container group exists but does not reference the requested pinned image."
}
if ([int]$Verified.replicas -ne 1) {
    throw "Salad container group exists but does not retain one replica."
}
$VerifiedState = $Verified.PSObject.Properties["current_state"]
if ($null -ne $VerifiedState -and $null -ne $VerifiedState.Value) {
    $StateStatus = [string]$VerifiedState.Value.status
    $StateDescription = [string]$VerifiedState.Value.description
    if ($StateStatus -in @("failed", "error")) {
        if ($StateDescription -match "Manifest Not Found") {
            throw (
                "Salad cannot pull the configured private GHCR image (Manifest Not Found). " +
                "Verify that GHCR_USERNAME is the GitHub username paired with " +
                "GHCR_READ_PACKAGES_PAT, that the token has read:packages access, and that " +
                "the pinned image exists. container_image=$PinnedImage"
            )
        }
        throw "Salad image upscaler entered $StateStatus: $StateDescription"
    }
}
Write-Host "Image upscaler deployment submitted and verified over HTTPS." -ForegroundColor Green
Write-Host "Run this script with -Action Status to obtain IMAGE_UPSCALER_URL."
