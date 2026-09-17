[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(15, 300)]
    [int]$TimeoutSeconds = 120,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ServicesPath = Join-Path $RepoRoot "deploy\salad\services.json"
if (-not (Test-Path -LiteralPath $ServicesPath -PathType Leaf)) {
    throw "Salad service manifest not found: $ServicesPath"
}

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

function Get-Status {
    param([Parameter(Mandatory)][object]$Group)

    if ($null -eq $Group.current_state) {
        return "unknown"
    }
    return [string]$Group.current_state.status
}

Import-EnvFile -Path $EnvFile

$Document = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$ServiceProperty = $Document.services.PSObject.Properties[$Service]
if ($null -eq $ServiceProperty) {
    $Available = @($Document.services.PSObject.Properties.Name) -join ", "
    throw "Unknown Salad service '$Service'. Available services: $Available"
}
$Definition = $ServiceProperty.Value
$Organization = [string]$Document.stack.organization
$Project = [string]$Document.stack.project
$GroupName = [string]$Definition.group_name
$QueueName = [string]$Definition.queue_name

if ([int]$Definition.autoscaler.min_replicas -ne 0) {
    throw "Service '$Service' is not configured for scale-to-zero (min_replicas must be 0)."
}

$ApiKey = [Environment]::GetEnvironmentVariable(
    "SALAD_API_KEY",
    [EnvironmentVariableTarget]::Process
)
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    if ($NonInteractive) {
        throw "SALAD_API_KEY is missing and -NonInteractive was requested."
    }
    $SecureValue = Read-Host "Salad API key" -AsSecureString
    $Credential = [PSCredential]::new("salad", $SecureValue)
    $ApiKey = $Credential.GetNetworkCredential().Password
}
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "SALAD_API_KEY is empty."
}

$Headers = @{
    "Salad-Api-Key" = $ApiKey.Trim()
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-scale-to-zero-arm/1.0"
}
$ApiBase = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$GroupUri = "$ApiBase/containers/$GroupName"
$QueueUri = "$ApiBase/queues/$QueueName"

# Fail before changing group state if the queue attachment target does not exist.
Invoke-RestMethod -Uri $QueueUri -Headers $Headers -TimeoutSec 30 | Out-Null
Write-Host "Queue exists: $QueueName" -ForegroundColor Green

function Get-Group {
    return Invoke-RestMethod -Uri $GroupUri -Headers $Headers -TimeoutSec 30
}

$Group = Get-Group
$Status = Get-Status -Group $Group
if ($Status -eq "stopped") {
    Invoke-RestMethod `
        -Method Post `
        -Uri "$GroupUri/start" `
        -Headers $Headers `
        -TimeoutSec 60 |
        Out-Null
}
elseif ($Status -notin @("deploying", "running")) {
    throw "Container group '$GroupName' cannot be armed from status '$Status'."
}

$Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    $Group = Get-Group
    $Status = Get-Status -Group $Group
    Write-Host (
        "{0} service={1} status={2} replicas={3} pending={4}" -f `
        (Get-Date -Format "HH:mm:ss"),
        $Service,
        $Status,
        [int]$Group.replicas,
        [bool]$Group.pending_change
    )

    if (-not [bool]$Group.pending_change -and $Status -in @("deploying", "running")) {
        Write-Host (
            "{0} scale-to-zero armed: status={1}, replicas={2}; queue work may now autoscale it." -f `
            $Service,
            $Status,
            [int]$Group.replicas
        ) -ForegroundColor Green
        exit 0
    }

    Start-Sleep -Seconds 5
}
while ((Get-Date) -lt $Deadline)

throw (
    "Container group '$GroupName' did not become scale-to-zero armed before timeout; " +
    "last status='$Status' replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
)
