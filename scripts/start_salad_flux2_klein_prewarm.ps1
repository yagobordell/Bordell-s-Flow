[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [ValidateRange(10, 120)]
    [int]$TimeoutMinutes = 60,

    [string]$MetricsOutput = "",

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$Service = "flux2_klein"
$BenchmarkStarted = Get-Date
$FirstInstanceAt = $null
$DownloadStartedAt = $null
$ContainerStartedAt = $null
$ReadyAt = $null

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
            "FLUX.2 Klein prewarm queue inspection: page $Page " +
            "($([int]$SecondsRemaining)s remaining in safety check)..."
        )
        $Response = Invoke-RestMethod `
            -Uri "$QueueUrl/jobs?page=$Page&page_size=25" `
            -Headers $Headers `
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
    $Response = Invoke-RestMethod -Uri $InstancesUrl -Headers $Headers -TimeoutSec 30
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
    throw "FLUX.2 Klein prewarm requires manifest min_replicas=0."
}
if ([int]$Definition.autoscaler.max_replicas -ne 1) {
    throw "FLUX.2 Klein prewarm requires manifest max_replicas=1."
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
    "User-Agent" = "ai-video-factory-flux2-klein-prewarm/1.2"
}

$Queue = Get-Queue
$QueueInspectionDeadline = (Get-Date).AddMinutes(2)
$QueueSnapshot = Get-QueueActiveSnapshot -Deadline $QueueInspectionDeadline
if (-not [bool]$QueueSnapshot.complete) {
    throw (
        "FLUX.2 Klein prewarm could not exhaustively inspect queue jobs before GPU allocation; " +
        "refusing to start a replica."
    )
}
$ActiveQueueJobs = @($QueueSnapshot.active_jobs)
if ($ActiveQueueJobs.Count -gt 0) {
    $ActiveDescription = ($ActiveQueueJobs | ForEach-Object {
        "transport=$([string]$_.id) status=$([string]$_.status)"
    }) -join "; "
    throw "FLUX.2 Klein prewarm requires no pending/running queue jobs; found: $ActiveDescription"
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
        "FLUX.2 Klein prewarm requires '$GroupName' stopped at replicas=0/pending=False; " +
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

Write-Host "FLUX.2 Klein prewarm: requesting exactly one replica before queue submission." -ForegroundColor Cyan
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
    throw "Salad did not persist the one-replica FLUX.2 Klein prewarm state."
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
    $Instances = @(Get-Instances)
    $Status = [string]$Group.current_state.status

    if ($Status -eq "failed") {
        throw "FLUX container group entered failed state during prewarm."
    }
    if ([int]$Group.replicas -gt 1 -or $Instances.Count -gt 1) {
        throw "FLUX.2 Klein prewarm refuses more than one replica."
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

    if ($Instances.Count -eq 1) {
        if ($null -eq $FirstInstanceAt) {
            $FirstInstanceAt = Get-Date
        }
        if ($State -eq "downloading" -and $null -eq $DownloadStartedAt) {
            $DownloadStartedAt = Get-Date
        }
        if ($Started -and $null -eq $ContainerStartedAt) {
            $ContainerStartedAt = Get-Date
        }
        if ($Ready -and $null -eq $ReadyAt) {
            $ReadyAt = Get-Date
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
    throw "FLUX.2 Klein prewarm did not reach one started ready replica before timeout."
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
        if (-not [string]::IsNullOrWhiteSpace($MetricsOutput)) {
            $ResolvedMetrics = $MetricsOutput
            if (-not [IO.Path]::IsPathRooted($ResolvedMetrics)) {
                $ResolvedMetrics = Join-Path $RepoRoot $ResolvedMetrics
            }
            $MetricsDir = Split-Path -Parent $ResolvedMetrics
            if (-not [string]::IsNullOrWhiteSpace($MetricsDir)) {
                New-Item -ItemType Directory -Force -Path $MetricsDir | Out-Null
            }
            $ReadyTimestamp = if ($null -ne $ReadyAt) { $ReadyAt } else { Get-Date }
            $FirstTimestamp = if ($null -ne $FirstInstanceAt) { $FirstInstanceAt } else { $ReadyTimestamp }
            $ContainerTimestamp = if ($null -ne $ContainerStartedAt) {
                $ContainerStartedAt
            }
            else {
                $ReadyTimestamp
            }
            $DownloadTimestamp = if ($null -ne $DownloadStartedAt) {
                $DownloadStartedAt
            }
            else {
                $FirstTimestamp
            }
            $Metrics = [ordered]@{
                schema_version = "1"
                service = $Service
                benchmark_started_utc = $BenchmarkStarted.ToUniversalTime().ToString("o")
                node_assignment_seconds = [Math]::Round(
                    ($FirstTimestamp - $BenchmarkStarted).TotalSeconds,
                    3
                )
                image_download_seconds = [Math]::Round(
                    ($ContainerTimestamp - $DownloadTimestamp).TotalSeconds,
                    3
                )
                image_download_state_observed = ($null -ne $DownloadStartedAt)
                model_bootstrap_seconds = [Math]::Round(
                    ($ReadyTimestamp - $ContainerTimestamp).TotalSeconds,
                    3
                )
                time_to_ready_seconds = [Math]::Round(
                    ($ReadyTimestamp - $BenchmarkStarted).TotalSeconds,
                    3
                )
                machine_id = if ($Instances.Count -eq 1) { [string]$Instances[0].machine_id } else { "" }
            }
            $Metrics | ConvertTo-Json -Depth 5 |
                Set-Content -LiteralPath $ResolvedMetrics -Encoding utf8
            Write-Host "FLUX.2 Klein prewarm metrics: $ResolvedMetrics" -ForegroundColor Green
        }
        Write-Host "FLUX.2 Klein prewarm complete: one ready replica held for fallback queue work." -ForegroundColor Green
        exit 0
    }
}
while ((Get-Date) -lt $HoldDeadline)

throw "FLUX ready replica could not be held without losing readiness."
