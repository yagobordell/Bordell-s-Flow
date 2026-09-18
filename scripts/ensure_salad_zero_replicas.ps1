[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "ideogram4", "flux2_klein", "ltx25")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(1, 30)]
    [int]$TimeoutMinutes = 5,

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
        return $Value.Trim()
    }
    if ($NonInteractive) {
        throw "$Name is missing and -NonInteractive was requested."
    }

    if ($Secret) {
        $SecureValue = Read-Host $Prompt -AsSecureString
        $Credential = [PSCredential]::new("salad-zero-replicas", $SecureValue)
        $Value = $Credential.GetNetworkCredential().Password
    }
    else {
        $Value = Read-Host $Prompt
    }
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is empty."
    }

    $Value = $Value.Trim()
    [Environment]::SetEnvironmentVariable(
        $Name,
        $Value,
        [EnvironmentVariableTarget]::Process
    )
    return $Value
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
$Organization = [string]$Document.stack.organization
$Project = [string]$Document.stack.project
$GroupName = [string]$Definition.group_name
$Base = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$GroupUrl = "$Base/containers/$GroupName"
$Headers = @{
    "Salad-Api-Key" = Get-Setting -Name "SALAD_API_KEY" -Prompt "Salad API key" -Secret
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-zero-replicas/1.1"
}

function Get-Group {
    try {
        return Invoke-RestMethod -Uri $GroupUrl -Headers $Headers -TimeoutSec 30
    }
    catch {
        $Response = $_.Exception.Response
        if ($null -ne $Response) {
            try {
                if ([int]$Response.StatusCode -eq 404) {
                    return $null
                }
            }
            catch {
            }
        }
        throw
    }
}

function Wait-ForStoppedGroup {
    param([Parameter(Mandatory)][object]$InitialGroup)

    $Group = $InitialGroup
    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while ($true) {
        $Status = [string]$Group.current_state.status
        $Replicas = [int]$Group.replicas
        $Pending = [bool]$Group.pending_change

        if ($Status -eq "stopped" -and -not $Pending) {
            return $Group
        }
        if ((Get-Date) -ge $Deadline) {
            throw (
                "Container group '$GroupName' did not reach stopped state before timeout. " +
                "Current status=$Status replicas=$Replicas pending=$Pending."
            )
        }

        Write-Host (
            "{0} service={1} waiting-for-stopped status={2} replicas={3} pending={4}" -f `
            (Get-Date -Format "HH:mm:ss"),
            $Service,
            $Status,
            $Replicas,
            $Pending
        )
        Start-Sleep -Seconds 5
        $Group = Get-Group
        if ($null -eq $Group) {
            throw "Container group '$GroupName' disappeared while waiting for stopped state."
        }
    }
}

$Group = Get-Group
if ($null -eq $Group) {
    Write-Host "$Service worker group does not exist; zero-replica guard not needed." -ForegroundColor Green
    exit 0
}

$Status = [string]$Group.current_state.status
if ($Status -ne "stopped" -or [bool]$Group.pending_change) {
    Write-Host (
        "Waiting for '$GroupName' to settle as stopped before normalizing replicas..."
    ) -ForegroundColor Cyan
    $Group = Wait-ForStoppedGroup -InitialGroup $Group
}

if ([int]$Group.replicas -eq 0) {
    Write-Host "$Service worker is stopped at zero replicas." -ForegroundColor Green
    exit 0
}

Write-Host (
    "Normalizing stopped group '$GroupName' replicas from $([int]$Group.replicas) to 0..."
) -ForegroundColor Cyan

$Body = @{ replicas = 0 } | ConvertTo-Json
Invoke-RestMethod `
    -Method Patch `
    -Uri $GroupUrl `
    -Headers $Headers `
    -ContentType "application/merge-patch+json" `
    -Body $Body `
    -TimeoutSec 60 |
    Out-Null

$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    if ($null -eq $Group) {
        throw "Container group '$GroupName' disappeared while normalizing replicas."
    }

    $Status = [string]$Group.current_state.status
    $Replicas = [int]$Group.replicas
    Write-Host (
        "{0} service={1} status={2} replicas={3} pending={4}" -f `
        (Get-Date -Format "HH:mm:ss"),
        $Service,
        $Status,
        $Replicas,
        [bool]$Group.pending_change
    )

    if ($Status -eq "stopped" -and $Replicas -eq 0 -and -not [bool]$Group.pending_change) {
        Write-Host "$Service worker is stopped at zero replicas." -ForegroundColor Green
        exit 0
    }
}
while ((Get-Date) -lt $Deadline)

throw (
    "Container group '$GroupName' did not settle at stopped/replicas=0 before timeout."
)
