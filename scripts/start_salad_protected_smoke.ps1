[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "ideogram4", "ltx25")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(1, 15)]
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
    $Credential = [PSCredential]::new("salad-protected-smoke", $SecureValue)
    $Value = $Credential.GetNetworkCredential().Password
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "SALAD_API_KEY is empty."
    }
    return $Value.Trim()
}

function Get-Group {
    return Invoke-RestMethod -Uri $GroupUrl -Headers $Headers -TimeoutSec 30
}

function Get-Queue {
    return Invoke-RestMethod -Uri $QueueUrl -Headers $Headers -TimeoutSec 30
}

function Get-Instances {
    $Response = Invoke-RestMethod -Uri $InstancesUrl -Headers $Headers -TimeoutSec 30
    if ($Response.PSObject.Properties.Name -contains "instances") {
        return @($Response.instances)
    }
    if ($Response.PSObject.Properties.Name -contains "items") {
        return @($Response.items)
    }
    return @()
}

function Test-QueueAttachment {
    param([Parameter(Mandatory)][object]$Queue)

    return @(
        @($Queue.container_groups) |
            Where-Object { [string]$_.name -eq $GroupName }
    ).Count -eq 1
}

function New-BootstrapAutoscaler {
    return @{
        min_replicas = 1
        max_replicas = [int]$Definition.autoscaler.max_replicas
        desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
        polling_period = [int]$Definition.autoscaler.polling_period
        max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
        max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
    }
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
    throw "Protected smoke bootstrap requires manifest min_replicas=0 for '$Service'."
}
if ([int]$Definition.autoscaler.max_replicas -lt 1) {
    throw "Protected smoke bootstrap requires max_replicas>=1 for '$Service'."
}

$Organization = [string]$Document.stack.organization
$Project = [string]$Document.stack.project
$GroupName = [string]$Definition.group_name
$QueueName = [string]$Definition.queue_name
$BaseUrl = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$GroupUrl = "$BaseUrl/containers/$GroupName"
$InstancesUrl = "$GroupUrl/instances"
$QueueUrl = "$BaseUrl/queues/$QueueName"
$Headers = @{
    "Salad-Api-Key" = Get-SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-protected-smoke-bootstrap/1.0"
}

$Queue = Get-Queue
if ([int]$Queue.current_queue_length -ne 0) {
    throw (
        "Protected smoke requires an empty queue before bootstrap; " +
        "'$QueueName' contains $([int]$Queue.current_queue_length) job(s)."
    )
}

$Group = Get-Group
$Status = [string]$Group.current_state.status
if ($Status -ne "stopped" -or [bool]$Group.pending_change -or [int]$Group.replicas -ne 0) {
    throw (
        "Protected smoke bootstrap requires '$GroupName' at stopped/replicas=0/pending=False; " +
        "status=$Status replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
    )
}
if ([string]$Group.queue_connection.queue_name -ne $QueueName) {
    throw "Container group '$GroupName' is configured for an unexpected queue."
}

$Body = @{ queue_autoscaler = New-BootstrapAutoscaler } | ConvertTo-Json -Depth 10
Write-Host (
    "$Service protected smoke temporarily setting queue_autoscaler.min_replicas=1."
) -ForegroundColor Cyan
Invoke-RestMethod `
    -Method Patch `
    -Uri $GroupUrl `
    -Headers $Headers `
    -ContentType "application/merge-patch+json" `
    -Body $Body `
    -TimeoutSec 60 |
    Out-Null

$PatchDeadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.queue_autoscaler.min_replicas -eq 1
    ) {
        break
    }
}
while ((Get-Date) -lt $PatchDeadline)
if ([int]$Group.queue_autoscaler.min_replicas -ne 1 -or [bool]$Group.pending_change) {
    throw "Salad did not persist protected smoke min_replicas=1 before Start."
}

Invoke-RestMethod `
    -Method Post `
    -Uri "$GroupUrl/start" `
    -Headers $Headers `
    -TimeoutSec 60 |
    Out-Null

$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    $Queue = Get-Queue
    $Instances = @(Get-Instances)
    $Status = [string]$Group.current_state.status
    $Attached = Test-QueueAttachment -Queue $Queue
    $StartedInstances = @($Instances | Where-Object { [bool]$_.started })

    Write-Host (
        "{0} service={1} status={2} replicas={3} instances={4} started={5} attached={6} pending={7}" -f
        (Get-Date -Format "HH:mm:ss"),
        $Service,
        $Status,
        [int]$Group.replicas,
        $Instances.Count,
        $StartedInstances.Count,
        $Attached,
        [bool]$Group.pending_change
    )

    if ($Status -eq "failed") {
        throw "Container group '$GroupName' entered failed state during protected bootstrap."
    }
    if ([int]$Group.replicas -gt 1 -or $Instances.Count -gt 1) {
        throw (
            "Protected smoke refuses more than one bootstrap replica; " +
            "replicas=$([int]$Group.replicas) instances=$($Instances.Count)."
        )
    }
    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.queue_autoscaler.min_replicas -eq 1 -and
        $Instances.Count -eq 1 -and
        $StartedInstances.Count -eq 1 -and
        $Attached
    ) {
        Write-Warning (
            "$Service protected bootstrap verified one started instance and queue attachment; " +
            "the caller must restore min_replicas=0 and stop in a finally block."
        )
        exit 0
    }
}
while ((Get-Date) -lt $Deadline)

throw (
    "Container group did not expose one started bootstrap instance with verified queue attachment " +
    "before timeout."
)
