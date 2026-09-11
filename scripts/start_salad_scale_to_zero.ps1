[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "ideogram4", "ltx25")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(1, 10)]
    [int]$TimeoutMinutes = 2,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"

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
        $Value = $Matches["value"].Trim()
        if ($Value.Length -ge 2) {
            $First = $Value.Substring(0, 1)
            $Last = $Value.Substring($Value.Length - 1, 1)
            if (($First -eq '"' -and $Last -eq '"') -or ($First -eq "'" -and $Last -eq "'")) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }
        }

        $Existing = [Environment]::GetEnvironmentVariable(
            $Name,
            [EnvironmentVariableTarget]::Process
        )
        if ([string]::IsNullOrWhiteSpace($Existing)) {
            [Environment]::SetEnvironmentVariable(
                $Name,
                $Value,
                [EnvironmentVariableTarget]::Process
            )
        }
    }
}

function Get-SaladApiKey {
    $Value = [Environment]::GetEnvironmentVariable(
        "SALAD_API_KEY",
        [EnvironmentVariableTarget]::Process
    )
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value.Trim()
    }
    if ($NonInteractive) {
        throw "SALAD_API_KEY is missing and -NonInteractive was requested."
    }

    $SecureValue = Read-Host "Salad API key" -AsSecureString
    $Credential = [PSCredential]::new("salad-start", $SecureValue)
    $Value = $Credential.GetNetworkCredential().Password
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "SALAD_API_KEY is empty."
    }
    return $Value.Trim()
}

function Get-Group {
    return Invoke-RestMethod `
        -Uri $GroupUrl `
        -Headers $Headers `
        -TimeoutSec 30
}

function Test-ScaleToZeroActive {
    param([Parameter(Mandatory)][object]$Group)

    $Status = [string]$Group.current_state.status
    if ($Status -eq "running" -and -not [bool]$Group.pending_change) {
        return $true
    }

    return (
        [int]$Definition.autoscaler.min_replicas -eq 0 -and
        [int]$Group.replicas -eq 0 -and
        -not [bool]$Group.pending_change -and
        $Status -eq "deploying"
    )
}

Import-EnvFile -Path $EnvFile
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad stack manifest not found: $ManifestPath"
}

$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$ServiceProperty = $Document.services.PSObject.Properties[$Service]
if ($null -eq $ServiceProperty) {
    throw "Unknown Salad service '$Service'."
}
$Definition = $ServiceProperty.Value
if ([int]$Definition.autoscaler.min_replicas -ne 0) {
    throw "Scale-to-zero starter requires min_replicas=0 for '$Service'."
}

$Organization = [string]$Document.stack.organization
$Project = [string]$Document.stack.project
$GroupName = [string]$Definition.group_name
$GroupUrl = (
    "https://api.salad.com/api/public/organizations/{0}/projects/{1}/containers/{2}" -f
    $Organization,
    $Project,
    $GroupName
)
$Headers = @{
    "Salad-Api-Key" = Get-SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-scale-to-zero-starter/1.0"
}

$Group = Get-Group
if (Test-ScaleToZeroActive -Group $Group) {
    Write-Host (
        "{0} scale-to-zero active: status={1} replicas={2} pending={3}" -f
        $Service,
        [string]$Group.current_state.status,
        [int]$Group.replicas,
        [bool]$Group.pending_change
    ) -ForegroundColor Green
    exit 0
}

if ([string]$Group.current_state.status -eq "failed") {
    throw "Container group '$GroupName' is failed before Start."
}

if ([string]$Group.current_state.status -eq "stopped") {
    Invoke-RestMethod `
        -Method Post `
        -Uri "$GroupUrl/start" `
        -Headers $Headers `
        -TimeoutSec 60 |
        Out-Null
}

$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    $Status = [string]$Group.current_state.status
    Write-Host (
        "{0} service={1} status={2} replicas={3} pending={4} description={5}" -f
        (Get-Date -Format "HH:mm:ss"),
        $Service,
        $Status,
        [int]$Group.replicas,
        [bool]$Group.pending_change,
        [string]$Group.current_state.description
    )

    if ($Status -eq "failed") {
        throw "Container group '$GroupName' entered failed state during Start."
    }
    if (Test-ScaleToZeroActive -Group $Group) {
        Write-Host (
            "$Service scale-to-zero start accepted; the first queued job may trigger a cold start."
        ) -ForegroundColor Green
        exit 0
    }
}
while ((Get-Date) -lt $Deadline)

throw "Container group did not activate scale-to-zero state before timeout."
