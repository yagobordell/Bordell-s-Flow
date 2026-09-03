[CmdletBinding()]
param(
    [string]$Image = (
        "docker.io/yagobordell/ai-video-factory-benchmark:" +
        "phase7-ltx25-torch211-cu128-natten0216-v9-salad"
    ),

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [switch]$SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Organization = "yagobordellorg"
$Project = "aivideofactory"
$GroupName = "ai-video-factory-bench-rtx5090"
$RequiredCommit = "2da9bf0179c4ba0a0c0ac3428f79f522345f0065"
$ContainersBase = (
    "https://api.salad.com/api/public/organizations/" +
    "$Organization/projects/$Project/containers"
)

function Get-Setting {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Prompt,
        [switch]$Secret
    )

    $Value = [Environment]::GetEnvironmentVariable(
        $Name,
        [EnvironmentVariableTarget]::Process
    )
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value
    }

    if ($Secret) {
        $SecureValue = Read-Host $Prompt -AsSecureString
        $Credential = [PSCredential]::new("phase7", $SecureValue)
        $Value = $Credential.GetNetworkCredential().Password
    }
    else {
        $Value = Read-Host $Prompt
    }

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is empty."
    }
    return $Value.Trim()
}

function Get-GroupStatus {
    param([Parameter(Mandatory)][object]$Group)

    if ($null -eq $Group.current_state) {
        return "unknown"
    }
    return [string]$Group.current_state.status
}

Set-Location ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..")))

& git merge-base --is-ancestor $RequiredCommit HEAD
if ($LASTEXITCODE -ne 0) {
    throw "Repository is outdated. Run: git pull --ff-only origin main"
}

if (-not (Test-Path -LiteralPath ".\docker\phase7-benchmark\assets\shot_001.png")) {
    throw "The canonical keyframe is missing. Run resume_phase7_benchmark.ps1 -Action RecoverKeyframe."
}

if (-not $SkipBuild) {
    & docker version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop is not running."
    }

    Write-Host "Building and publishing $Image" -ForegroundColor Cyan
    & docker buildx build `
        --platform linux/amd64 `
        --file ".\docker\phase7-benchmark\Dockerfile" `
        --tag $Image `
        --push `
        .
    if ($LASTEXITCODE -ne 0) {
        throw "Docker build or push failed."
    }

    & docker buildx imagetools inspect $Image *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "The published image cannot be inspected."
    }
}

$SaladApiKey = Get-Setting `
    -Name "SALAD_API_KEY" `
    -Prompt "Salad API key" `
    -Secret
$R2Endpoint = Get-Setting `
    -Name "R2_ENDPOINT_URL" `
    -Prompt "R2 endpoint URL"
$R2Bucket = Get-Setting `
    -Name "R2_BUCKET" `
    -Prompt "R2 bucket"
$R2AccessKey = Get-Setting `
    -Name "R2_ACCESS_KEY_ID" `
    -Prompt "R2 access key ID" `
    -Secret
$R2SecretKey = Get-Setting `
    -Name "R2_SECRET_ACCESS_KEY" `
    -Prompt "R2 secret access key" `
    -Secret
$R2Prefix = [Environment]::GetEnvironmentVariable(
    "PHASE7_BENCHMARK_R2_PREFIX",
    [EnvironmentVariableTarget]::Process
)
if ([string]::IsNullOrWhiteSpace($R2Prefix)) {
    $R2Prefix = "phase7/benchmarks/rtx5090-cloud"
}

$Headers = @{
    "Salad-Api-Key" = $SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-phase7-deploy/1.0"
}

$Group = Invoke-RestMethod `
    -Uri "$ContainersBase/$GroupName" `
    -Headers $Headers `
    -TimeoutSec 30

if ($Group.pending_change) {
    throw "The container group already has a pending change."
}
if ((Get-GroupStatus $Group) -ne "stopped") {
    throw "The container group must be stopped before upgrading it."
}

$PreviousVersion = [int]$Group.version
if ($Group.container.image -eq $Image) {
    Write-Host "Image already prepared in Salad." -ForegroundColor Green
    exit 0
}

$PatchBody = @{
    container = @{
        image = $Image
        environment_variables = @{
            R2_ENDPOINT_URL = $R2Endpoint
            R2_BUCKET = $R2Bucket
            R2_ACCESS_KEY_ID = $R2AccessKey
            R2_SECRET_ACCESS_KEY = $R2SecretKey
            PHASE7_BENCHMARK_R2_PREFIX = $R2Prefix
        }
    }
} | ConvertTo-Json -Depth 20

Write-Host "Submitting Salad image upgrade..." -ForegroundColor Cyan
Invoke-RestMethod `
    -Method Patch `
    -Uri "$ContainersBase/$GroupName" `
    -Headers $Headers `
    -ContentType "application/merge-patch+json" `
    -Body $PatchBody `
    -TimeoutSec 60 |
    Out-Null

$Deadline = (Get-Date).AddMinutes($PrepareTimeoutMinutes)
do {
    Start-Sleep -Seconds 30
    $Group = Invoke-RestMethod `
        -Uri "$ContainersBase/$GroupName" `
        -Headers $Headers `
        -TimeoutSec 30

    $Description = [string]$Group.current_state.description
    Write-Host (
        "{0} version={1} pending={2} status={3} description={4}" -f `
        (Get-Date -Format "HH:mm:ss"),
        $Group.version,
        $Group.pending_change,
        (Get-GroupStatus $Group),
        $Description
    )
}
while ($Group.pending_change -and (Get-Date) -lt $Deadline)

if ($Group.pending_change) {
    throw "Salad did not finish preparing the image before the timeout."
}
if ($Group.current_state.description -match "Image Not Found") {
    throw "Salad could not download the image."
}
if ($Group.container.image -ne $Image) {
    throw "Salad did not activate the expected image. Received: $($Group.container.image)"
}
if ([int]$Group.version -le $PreviousVersion) {
    throw "The container group version did not increase."
}

$EnvironmentNames = @($Group.container.environment_variables.PSObject.Properties.Name)
foreach ($Name in @(
    "R2_ENDPOINT_URL",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "PHASE7_BENCHMARK_R2_PREFIX"
)) {
    if ($EnvironmentNames -notcontains $Name) {
        throw "Salad did not retain environment variable $Name."
    }
}

$PatchBody = $null
$R2SecretKey = $null
$R2AccessKey = $null
$SaladApiKey = $null

Write-Host "Phase 7 benchmark v9 is prepared and stopped." -ForegroundColor Green
$Group |
    Select-Object `
        name,
        version,
        replicas,
        pending_change,
        @{Name = "Status"; Expression = {$_.current_state.status}},
        @{Name = "Image"; Expression = {$_.container.image}} |
    Format-List

Write-Host "Next: run resume_phase7_benchmark.ps1 -Action Start" -ForegroundColor Cyan
