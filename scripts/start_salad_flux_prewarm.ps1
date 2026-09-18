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
$Service = "flux2_klein"

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

function Get-HttpStatusCode {
    param([Parameter(Mandatory)][object]$ErrorRecord)

    $Response = $ErrorRecord.Exception.Response
    if ($null -eq $Response) {
        return $null
    }
    try {
        return [int]$Response.StatusCode
    }
    catch {
        return $null
    }
}

function Test-TransientSaladFailure {
    param([Parameter(Mandatory)][object]$ErrorRecord)

    $StatusCode = Get-HttpStatusCode -ErrorRecord $ErrorRecord
    if ($StatusCode -in @(408, 429, 500, 502, 503, 504)) {
        return $true
    }

    $Message = [string]$ErrorRecord.Exception.Message
    return $Message -match (
        "(?i)timed out|timeout|upstream connect error|disconnect/reset|" +
        "remote connection failure|server unavailable|gateway timeout"
    )
}

function Invoke-SaladRequest {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Operation,
        [string]$Method = "Get",
        [string]$ContentType = "",
        [string]$Body = "",
        [ValidateRange(1, 120)][int]$TimeoutSec = 30,
        [ValidateRange(1, 10)][int]$MaxAttempts = 6
    )

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt += 1) {
        try {
            $Arguments = @{
                Method = $Method
                Uri = $Uri
                Headers = $Headers
                TimeoutSec = $TimeoutSec
            }
            if (-not [string]::IsNullOrWhiteSpace($ContentType)) {
                $Arguments["ContentType"] = $ContentType
            }
            if (-not [string]::IsNullOrWhiteSpace($Body)) {
                $Arguments["Body"] = $Body
            }
            return Invoke-RestMethod @Arguments
        }
        catch {
            if (-not (Test-TransientSaladFailure -ErrorRecord $_) -or $Attempt -ge $MaxAttempts) {
                throw
            }
            $DelaySeconds = [Math]::Min(15, 2 * $Attempt)
            Write-Warning (
                "FLUX Salad control-plane operation '$Operation' failed transiently " +
                "(attempt $Attempt/$MaxAttempts): $($_.Exception.Message). " +
                "Retrying in ${DelaySeconds}s without reallocating the worker."
            )
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    throw "Unreachable Salad retry state for '$Operation'."
}

function Get-Group {
    return Invoke-SaladRequest -Uri $GroupUrl -Operation "read container group"
}

function Get-Queue {
    return Invoke-SaladRequest -Uri $QueueUrl -Operation "read queue"
}

function Get-QueueActiveSnapshot {
    param([Parameter(Mandatory)][datetime]$Deadline)

    $Active = @()
    $Complete = $false
    $Pages = 0
    for ($Page = 1; $Page -le 100; $Page += 1) {
        if ((Get-Date) -ge $Deadline) {
            break
        }
        $SecondsRemaining = [Math]::Max(
            1,
            [Math]::Ceiling(($Deadline - (Get-Date)).TotalSeconds)
        )
        $RequestTimeoutSeconds = [int][Math]::Min(30, $SecondsRemaining)
        Write-Host (
            "FLUX prewarm queue inspection: page $Page " +
            "($([int]$SecondsRemaining)s remaining in safety check)..."
        )
        $Response = Invoke-SaladRequest `
            -Uri "$QueueUrl/jobs?page=$Page&page_size=25" `
            -Operation "inspect queue jobs page $Page" `
            -TimeoutSec $RequestTimeoutSeconds
        $Pages += 1
        $Items = @(
            if ($Response.PSObject.Properties.Name -contains "items") {
                $Response.items
            }
            elseif ($Response.PSObject.Properties.Name -contains "jobs") {
                $Response.jobs
            }
        )
        foreach ($Job in $Items) {
            if ([string]$Job.status -in @("pending", "running")) {
                $Active += $Job
            }
        }
        if ($Items.Count -lt 25) {
            $Complete = $true
            break
        }
    }

    return [PSCustomObject]@{
        active_jobs = @($Active)
        complete = $Complete
        pages = $Pages
    }
}

function Get-Instances {
    $Response = Invoke-SaladRequest -Uri $InstancesUrl -Operation "read container instances"
    if ($Response.PSObject.Properties.Name -contains "instances") {
        return @($Response.instances)
    }
    if ($Response.PSObject.Properties.Name -contains "items") {
        return @($Response.items)
    }
    return @()
}

function Get-QueueConnectionName {
    param([Parameter(Mandatory)][object]$Group)

    $ConnectionProperty = $Group.PSObject.Properties["queue_connection"]
    if ($null -eq $ConnectionProperty -or $null -eq $ConnectionProperty.Value) {
        return ""
    }
    $QueueNameProperty = $ConnectionProperty.Value.PSObject.Properties["queue_name"]
    if ($null -eq $QueueNameProperty -or $null -eq $QueueNameProperty.Value) {
        return ""
    }
    return [string]$QueueNameProperty.Value
}

function Get-VisibleAutoscalerMinReplicas {
    param([Parameter(Mandatory)][object]$Group)

    $AutoscalerProperty = $Group.PSObject.Properties["queue_autoscaler"]
    if ($null -eq $AutoscalerProperty -or $null -eq $AutoscalerProperty.Value) {
        return $null
    }
    $MinProperty = $AutoscalerProperty.Value.PSObject.Properties["min_replicas"]
    if ($null -eq $MinProperty -or $null -eq $MinProperty.Value) {
        return $null
    }
    return [int]$MinProperty.Value
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
    "User-Agent" = "ai-video-factory-flux-prewarm/1.2"
}

$Queue = Get-Queue
$QueueInspectionDeadline = (Get-Date).AddMinutes(2)
$QueueSnapshot = Get-QueueActiveSnapshot -Deadline $QueueInspectionDeadline
if (-not [bool]$QueueSnapshot.complete) {
    throw (
        "FLUX prewarm could not exhaustively inspect queue jobs before GPU allocation; " +
        "refusing to start a replica."
    )
}
$ActiveQueueJobs = @($QueueSnapshot.active_jobs)
if ($ActiveQueueJobs.Count -gt 0) {
    $ActiveDescription = ($ActiveQueueJobs | ForEach-Object {
        "transport=$([string]$_.id) status=$([string]$_.status)"
    }) -join "; "
    throw "FLUX prewarm requires no pending/running queue jobs; found: $ActiveDescription"
}
if ([int]$Queue.current_queue_length -ne 0) {
    Write-Warning (
        "FLUX queue summary is stale: current_queue_length=$([int]$Queue.current_queue_length), " +
        "but exhaustive enumeration found no pending or running jobs. Continuing safely."
    )
}

$Group = Get-Group
$Status = [string]$Group.current_state.status
if ($Status -ne "stopped" -or [bool]$Group.pending_change -or [int]$Group.replicas -ne 0) {
    throw (
        "FLUX prewarm requires '$GroupName' stopped at replicas=0/pending=False; " +
        "status=$Status replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
    )
}
$AttachedQueueName = Get-QueueConnectionName -Group $Group
if ([string]::IsNullOrWhiteSpace($AttachedQueueName)) {
    throw "FLUX container group response does not expose queue_connection.queue_name."
}
if ($AttachedQueueName -ne $QueueName) {
    throw (
        "FLUX container group is configured for unexpected queue '$AttachedQueueName'; " +
        "expected '$QueueName'."
    )
}

$PrewarmStartedAt = Get-Date
$AssignmentSeconds = $null
$ContainerStartedSeconds = $null
$ReadySeconds = $null

Write-Host "FLUX prewarm: requesting exactly one replica before queue submission." -ForegroundColor Cyan
Invoke-SaladRequest `
    -Method "Patch" `
    -Uri $GroupUrl `
    -Operation "request one FLUX replica" `
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

Invoke-SaladRequest `
    -Method "Post" `
    -Uri "$GroupUrl/start" `
    -Operation "start FLUX container group" `
    -TimeoutSec 60 |
    Out-Null

$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    $Instances = @(Get-Instances)
    $Status = [string]$Group.current_state.status

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
        $ElapsedSeconds = ((Get-Date) - $PrewarmStartedAt).TotalSeconds
        if ($null -eq $AssignmentSeconds) {
            $AssignmentSeconds = $ElapsedSeconds
        }
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
        if ($Started -and $null -eq $ContainerStartedSeconds) {
            $ContainerStartedSeconds = $ElapsedSeconds
        }
        if ($Ready -and $null -eq $ReadySeconds) {
            $ReadySeconds = $ElapsedSeconds
        }
    }

    Write-Host (
        "{0} service=flux2_klein status={1} state={2} started={3} ready={4} pulling_progress={5} machine={6}" -f `
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
Invoke-SaladRequest `
    -Method "Patch" `
    -Uri $GroupUrl `
    -Operation "hold ready FLUX replica" `
    -ContentType "application/merge-patch+json" `
    -Body (@{ queue_autoscaler = $Autoscaler } | ConvertTo-Json -Depth 10) `
    -TimeoutSec 60 |
    Out-Null

$HoldDeadline = (Get-Date).AddMinutes(2)
$VisibleMinReplicas = $null
do {
    Start-Sleep -Seconds 2
    $Group = Get-Group
    $Instances = @(Get-Instances)
    $VisibleMinReplicas = Get-VisibleAutoscalerMinReplicas -Group $Group
    $VisibleAutoscalerMatches = (
        $null -eq $VisibleMinReplicas -or
        [int]$VisibleMinReplicas -eq 1
    )
    if (
        -not [bool]$Group.pending_change -and
        $VisibleAutoscalerMatches -and
        [int]$Group.replicas -eq 1 -and
        $Instances.Count -eq 1 -and
        [bool]$Instances[0].started -and
        [bool]$Instances[0].ready
    ) {
        if ($null -eq $VisibleMinReplicas) {
            Write-Warning (
                "Salad GET does not expose queue_autoscaler for '$GroupName'; " +
                "warm hold is relying on the accepted autoscaler PATCH and settled group state."
            )
        }
        $BootstrapAfterStartSeconds = $null
        $ImagePullAndStartSeconds = $null
        if ($null -ne $ReadySeconds -and $null -ne $ContainerStartedSeconds) {
            $BootstrapAfterStartSeconds = $ReadySeconds - $ContainerStartedSeconds
        }
        if ($null -ne $ContainerStartedSeconds -and $null -ne $AssignmentSeconds) {
            $ImagePullAndStartSeconds = $ContainerStartedSeconds - $AssignmentSeconds
        }
        $PrewarmMetric = (
            "FLUX2_KLEIN_PREWARM_METRIC assignment_seconds={0:N1} " +
            "container_started_seconds={1:N1} image_pull_and_start_seconds={2:N1} " +
            "ready_seconds={3:N1} bootstrap_after_start_seconds={4:N1}"
        ) -f @(
            [double]$AssignmentSeconds,
            [double]$ContainerStartedSeconds,
            [double]$ImagePullAndStartSeconds,
            [double]$ReadySeconds,
            [double]$BootstrapAfterStartSeconds
        )
        Write-Host $PrewarmMetric
        Write-Host "FLUX prewarm complete: one ready replica held for fallback queue work." -ForegroundColor Green
        exit 0
    }
}
while ((Get-Date) -lt $HoldDeadline)

throw "FLUX ready replica could not be held without losing readiness."
