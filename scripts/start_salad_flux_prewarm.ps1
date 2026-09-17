[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [ValidateRange(10, 120)]
    [int]$TimeoutMinutes = 60,

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
    $Credential = [PSCredential]::new("salad-flux-prewarm", $SecureValue)
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

Import-EnvFile -Path $EnvFile
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad stack manifest not found: $ManifestPath"
}

$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$Definition = $Document.services.$Service
if ($null -eq $Definition) {
    throw "Manifest service '$Service' is missing."
}
if ([int]$Definition.autoscaler.min_replicas -ne 0) {
    throw "FLUX prewarm requires manifest min_replicas=0."
}
if ([int]$Definition.autoscaler.max_replicas -ne 1) {
    throw "FLUX prewarm requires manifest max_replicas=1."
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
    "User-Agent" = "ai-video-factory-flux-prewarm/1.0"
}

$Queue = Get-Queue
if ([int]$Queue.current_queue_length -ne 0) {
    throw "FLUX prewarm requires an empty queue; '$QueueName' contains $([int]$Queue.current_queue_length) job(s)."
}
$Attached = @(
    @($Queue.container_groups) |
        Where-Object { [string]$_.name -eq $GroupName }
).Count -eq 1
if (-not $Attached) {
    throw "FLUX queue '$QueueName' is not attached to container group '$GroupName'."
}

$Group = Get-Group
$Status = [string]$Group.current_state.status
if ($Status -ne "stopped" -or [bool]$Group.pending_change -or [int]$Group.replicas -ne 0) {
    throw (
        "FLUX prewarm requires '$GroupName' stopped at replicas=0/pending=False; " +
        "status=$Status replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
    )
}
if ([string]$Group.queue_connection.queue_name -ne $QueueName) {
    throw "FLUX container group is configured for an unexpected queue."
}

Write-Host "FLUX prewarm: requesting exactly one replica before queue submission." -ForegroundColor Cyan
Invoke-RestMethod `
    -Method Patch `
    -Uri $GroupUrl `
    -Headers $Headers `
    -ContentType "application/merge-patch+json" `
    -Body (@{ replicas = 1 } | ConvertTo-Json -Compress) `
    -TimeoutSec 60 |
    Out-Null

$PatchDeadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    if (-not [bool]$Group.pending_change -and [int]$Group.replicas -eq 1) {
        break
    }
}
while ((Get-Date) -lt $PatchDeadline)
if ([bool]$Group.pending_change -or [int]$Group.replicas -ne 1) {
    throw "Salad did not persist the one-replica FLUX prewarm state."
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

    if ([int]$Queue.current_queue_length -ne 0) {
        throw "FLUX prewarm detected queued work before readiness; refusing cold-start billing on a transport job."
    }
    if ($Status -eq "failed") {
        throw "FLUX container group entered failed state during prewarm."
    }
    if ([int]$Group.replicas -gt 1 -or $Instances.Count -gt 1) {
        throw "FLUX prewarm refuses more than one replica."
    }

    $State = "-"
    $Started = $false
    $Ready = $false
    $Pulling = "-"
    $Machine = ""
    if ($Instances.Count -eq 1) {
        $Instance = $Instances[0]
        if ($Instance.PSObject.Properties.Name -contains "state") {
            $State = [string]$Instance.state
        }
        if ($Instance.PSObject.Properties.Name -contains "started") {
            $Started = [bool]$Instance.started
        }
        if ($Instance.PSObject.Properties.Name -contains "ready") {
            $Ready = [bool]$Instance.ready
        }
        if ($Instance.PSObject.Properties.Name -contains "pulling_progress") {
            $Pulling = [string]$Instance.pulling_progress
        }
        if ($Instance.PSObject.Properties.Name -contains "machine_id") {
            $Machine = [string]$Instance.machine_id
        }
    }

    Write-Host (
        "{0} service=flux_schnell status={1} state={2} started={3} ready={4} pulling_progress={5} machine={6}" -f `
        (Get-Date -Format "HH:mm:ss"),
        $Status,
        $State,
        $Started,
        $Ready,
        $Pulling,
        $Machine
    )

    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.replicas -eq 1 -and
        $Instances.Count -eq 1 -and
        $Started -and
        $Ready
    ) {
        break
    }
}
while ((Get-Date) -lt $Deadline)

if (
    [bool]$Group.pending_change -or
    [int]$Group.replicas -ne 1 -or
    $Instances.Count -ne 1 -or
    -not $Started -or
    -not $Ready
) {
    throw "FLUX prewarm did not reach one started ready replica before timeout."
}

$Autoscaler = @{
    min_replicas = 1
    max_replicas = [int]$Definition.autoscaler.max_replicas
    desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
    polling_period = [int]$Definition.autoscaler.polling_period
    max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
    max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
}
Invoke-RestMethod `
    -Method Patch `
    -Uri $GroupUrl `
    -Headers $Headers `
    -ContentType "application/merge-patch+json" `
    -Body (@{ queue_autoscaler = $Autoscaler } | ConvertTo-Json -Depth 10) `
    -TimeoutSec 60 |
    Out-Null

$HoldDeadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 2
    $Group = Get-Group
    $Instances = @(Get-Instances)
    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.queue_autoscaler.min_replicas -eq 1 -and
        [int]$Group.replicas -eq 1 -and
        $Instances.Count -eq 1 -and
        [bool]$Instances[0].started -and
        [bool]$Instances[0].ready
    ) {
        Write-Host "FLUX prewarm complete: one ready replica held for fallback queue work." -ForegroundColor Green
        exit 0
    }
}
while ((Get-Date) -lt $HoldDeadline)

throw "FLUX ready replica could not be held without losing readiness."
