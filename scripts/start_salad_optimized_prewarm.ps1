[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "ltx25")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(10, 120)]
    [int]$TimeoutMinutes = 60,

    [switch]$HoldReadyReplica,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"

$Profiles = @{
    whisper = @{
        AllocatingSeconds = 180
        FinalAllocatingSeconds = 480
        MaxAllocatingReallocations = 2
        ImagePullStallSeconds = 120
        FinalImagePullStallSeconds = 360
        MaxImagePullReallocations = 2
        PostPullStartSeconds = 180
        FinalPostPullStartSeconds = 360
        MaxPostPullStartReallocations = 1
        RunningNotReadySeconds = 480
        FinalRunningNotReadySeconds = 900
        MaxRunningNotReadyReallocations = 1
        MaxNodeChanges = 6
    }
    breeze_tts2 = @{
        AllocatingSeconds = 240
        FinalAllocatingSeconds = 600
        MaxAllocatingReallocations = 2
        ImagePullStallSeconds = 180
        FinalImagePullStallSeconds = 480
        MaxImagePullReallocations = 2
        PostPullStartSeconds = 240
        FinalPostPullStartSeconds = 480
        MaxPostPullStartReallocations = 1
        RunningNotReadySeconds = 900
        FinalRunningNotReadySeconds = 1500
        MaxRunningNotReadyReallocations = 1
        MaxNodeChanges = 6
    }
    fish_speech = @{
        AllocatingSeconds = 300
        FinalAllocatingSeconds = 900
        MaxAllocatingReallocations = 2
        ImagePullStallSeconds = 240
        FinalImagePullStallSeconds = 600
        MaxImagePullReallocations = 2
        PostPullStartSeconds = 240
        FinalPostPullStartSeconds = 600
        MaxPostPullStartReallocations = 1
        RunningNotReadySeconds = 1200
        FinalRunningNotReadySeconds = 1800
        MaxRunningNotReadyReallocations = 1
        MaxNodeChanges = 6
    }
    ideogram4 = @{
        AllocatingSeconds = 300
        FinalAllocatingSeconds = 900
        MaxAllocatingReallocations = 2
        ImagePullStallSeconds = 180
        FinalImagePullStallSeconds = 480
        MaxImagePullReallocations = 2
        PostPullStartSeconds = 180
        FinalPostPullStartSeconds = 300
        MaxPostPullStartReallocations = 1
        RunningNotReadySeconds = 900
        FinalRunningNotReadySeconds = 900
        MaxRunningNotReadyReallocations = 1
        MaxNodeChanges = 2
    }
    ltx25 = @{
        AllocatingSeconds = 480
        FinalAllocatingSeconds = 1200
        MaxAllocatingReallocations = 2
        ImagePullStallSeconds = 300
        FinalImagePullStallSeconds = 900
        MaxImagePullReallocations = 2
        PostPullStartSeconds = 300
        FinalPostPullStartSeconds = 600
        MaxPostPullStartReallocations = 1
        RunningNotReadySeconds = 1800
        FinalRunningNotReadySeconds = 3600
        MaxRunningNotReadyReallocations = 1
        MaxNodeChanges = 6
    }
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
    $Credential = [PSCredential]::new("salad-optimized-prewarm", $SecureValue)
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

function Get-QueueJobSnapshot {
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

function Assert-QueueLogicallyEmpty {
    param(
        [Parameter(Mandatory)][object]$Queue,
        [ValidateRange(1, 120)][int]$VerificationSeconds = 30
    )

    $ReportedLength = [int]$Queue.current_queue_length
    if ($ReportedLength -eq 0) {
        return
    }

    $Snapshot = Get-QueueJobSnapshot -Deadline (Get-Date).AddSeconds($VerificationSeconds)
    if (-not [bool]$Snapshot.complete) {
        throw (
            "Optimized prewarm could not verify '$QueueName' after $([int]$Snapshot.pages) page(s); " +
            "refusing GPU allocation while queue state is ambiguous."
        )
    }
    $ActiveJobs = @($Snapshot.active_jobs)
    if ($ActiveJobs.Count -gt 0) {
        throw (
            "Optimized prewarm requires an empty queue; '$QueueName' has " +
            "$($ActiveJobs.Count) enumerable pending/running job(s)."
        )
    }

    Write-Warning (
        "$Service queue summary is stale during optimized prewarm: current_queue_length=" +
        "$ReportedLength, but exhaustive job enumeration found no pending or running jobs. " +
        "Treating the queue as logically empty."
    )
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

function Request-InstanceReallocation {
    param(
        [Parameter(Mandatory)][string]$InstanceId,
        [Parameter(Mandatory)][string]$Reason
    )

    Write-Warning "$Service requesting Salad node reallocation: $Reason"
    Invoke-RestMethod `
        -Method Post `
        -Uri "$InstancesUrl/$InstanceId/reallocate" `
        -Headers $Headers `
        -TimeoutSec 60 |
        Out-Null
}

function Test-QueueAttachment {
    param([Parameter(Mandatory)][object]$Queue)

    return @(
        @($Queue.container_groups) |
            Where-Object { [string]$_.name -eq $GroupName }
    ).Count -eq 1
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
$Profile = $Profiles[$Service]
if ([int]$Definition.autoscaler.min_replicas -ne 0) {
    throw "Optimized prewarm requires min_replicas=0 for '$Service'."
}
if ([int]$Definition.autoscaler.max_replicas -lt 1) {
    throw "Optimized prewarm requires max_replicas>=1 for '$Service'."
}
if ($Service -in @("ideogram4", "fish_speech") -and [int]$Definition.autoscaler.max_replicas -ne 1) {
    throw "$Service optimized prewarm requires max_replicas=1."
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
    "User-Agent" = "ai-video-factory-optimized-prewarm/1.4"
}

$Queue = Get-Queue
$null = Assert-QueueLogicallyEmpty -Queue $Queue

$Group = Get-Group
$Status = [string]$Group.current_state.status
if ($Status -ne "stopped" -or [bool]$Group.pending_change -or [int]$Group.replicas -ne 0) {
    throw (
        "Optimized prewarm requires '$GroupName' stopped at replicas=0/pending=False; " +
        "status=$Status replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
    )
}
if ([string]$Group.queue_connection.queue_name -ne $QueueName) {
    throw "Container group '$GroupName' is configured for an unexpected queue."
}
if ([int]$Group.queue_autoscaler.min_replicas -ne 0) {
    throw "Optimized prewarm refuses a remote autoscaler with min_replicas != 0."
}
if ($Service -in @("ideogram4", "fish_speech") -and [int]$Group.queue_autoscaler.max_replicas -ne 1) {
    throw "$Service optimized prewarm refuses a remote autoscaler with max_replicas != 1."
}

$TargetMinReplicas = if ($HoldReadyReplica) { 1 } else { 0 }
$ProfileLine = (
    (
        "Optimized prewarm profile: service={0} allocating={1}s/{2} retries " +
        "image_pull_stall={3}s/{4} retries post_pull_start={5}s/{6} retries " +
        "running_not_ready={7}s/{8} retries max_node_changes={9} hold_ready={10}"
    ) -f
    $Service,
    $Profile.AllocatingSeconds,
    $Profile.MaxAllocatingReallocations,
    $Profile.ImagePullStallSeconds,
    $Profile.MaxImagePullReallocations,
    $Profile.PostPullStartSeconds,
    $Profile.MaxPostPullStartReallocations,
    $Profile.RunningNotReadySeconds,
    $Profile.MaxRunningNotReadyReallocations,
    $Profile.MaxNodeChanges,
    [bool]$HoldReadyReplica
)
Write-Host $ProfileLine -ForegroundColor Cyan

$PrewarmStartedAt = Get-Date
$AssignmentSeconds = $null
$ContainerStartedSeconds = $null
$ReadySeconds = $null

$PrewarmPatch = @{ replicas = 1 }
if ($HoldReadyReplica) {
    $PrewarmPatch["queue_autoscaler"] = @{
        min_replicas = 1
        max_replicas = [int]$Definition.autoscaler.max_replicas
        desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
        polling_period = [int]$Definition.autoscaler.polling_period
        max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
        max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
    }
}
Invoke-RestMethod `
    -Method Patch `
    -Uri $GroupUrl `
    -Headers $Headers `
    -ContentType "application/merge-patch+json" `
    -Body ($PrewarmPatch | ConvertTo-Json -Depth 10 -Compress) `
    -TimeoutSec 60 |
    Out-Null

$PatchDeadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.replicas -eq 1 -and
        [int]$Group.queue_autoscaler.min_replicas -eq $TargetMinReplicas
    ) {
        break
    }
}
while ((Get-Date) -lt $PatchDeadline)
if (
    [bool]$Group.pending_change -or
    [int]$Group.replicas -ne 1 -or
    [int]$Group.queue_autoscaler.min_replicas -ne $TargetMinReplicas
) {
    throw "Salad did not persist the one-replica prewarm state safely."
}

Invoke-RestMethod `
    -Method Post `
    -Uri "$GroupUrl/start" `
    -Headers $Headers `
    -TimeoutSec 60 |
    Out-Null

$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$CurrentInstanceId = ""
$CurrentMachineId = ""
$LastObservedNonEmptyMachineId = ""
$NodeChanges = 0
$AllocatingSince = $null
$ImagePullSince = $null
$ImagePullBaseline = $null
$PostPullStartSince = $null
$RunningNotReadySince = $null
$AllocatingReallocations = 0
$ImagePullReallocations = 0
$PostPullStartReallocations = 0
$RunningNotReadyReallocations = 0
$ImagePullProgressThreshold = 0.005

while ((Get-Date) -lt $Deadline) {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    $Queue = Get-Queue
    $Instances = @(Get-Instances)
    $Status = [string]$Group.current_state.status
    $Attached = Test-QueueAttachment -Queue $Queue

    $ReportedQueueLength = [int]$Queue.current_queue_length
    if ($ReportedQueueLength -ne 0) {
        $null = Assert-QueueLogicallyEmpty -Queue $Queue
    }
    if ($Status -eq "failed") {
        throw "Container group '$GroupName' entered failed state during prewarm."
    }
    if ([int]$Group.replicas -gt 1 -or $Instances.Count -gt 1) {
        throw (
            "Optimized prewarm refuses more than one replica; " +
            "replicas=$([int]$Group.replicas) instances=$($Instances.Count)."
        )
    }

    $InstanceId = ""
    $MachineId = ""
    $InstanceState = "-"
    $PullingProgress = $null
    $Ready = $false
    $Started = $false
    if ($Instances.Count -eq 1) {
        $ElapsedSeconds = ((Get-Date) - $PrewarmStartedAt).TotalSeconds
        if ($null -eq $AssignmentSeconds) {
            $AssignmentSeconds = $ElapsedSeconds
        }
        $Instance = $Instances[0]
        if ($Instance.PSObject.Properties.Name -contains "id") {
            $InstanceId = [string]$Instance.id
        }
        if ($Instance.PSObject.Properties.Name -contains "machine_id") {
            $MachineId = [string]$Instance.machine_id
        }
        if ($Instance.PSObject.Properties.Name -contains "state") {
            $InstanceState = [string]$Instance.state
        }
        if ($Instance.PSObject.Properties.Name -contains "pulling_progress") {
            $PullingProgress = [double]$Instance.pulling_progress
        }
        if ($Instance.PSObject.Properties.Name -contains "ready") {
            $Ready = [bool]$Instance.ready
        }
        if ($Instance.PSObject.Properties.Name -contains "started") {
            $Started = [bool]$Instance.started
        }
    }

    $ObservedInstance = (
        $Instances.Count -eq 1 -and
        -not [string]::IsNullOrWhiteSpace($InstanceId)
    )

    if (-not [string]::IsNullOrWhiteSpace($MachineId)) {
        if (
            -not [string]::IsNullOrWhiteSpace($LastObservedNonEmptyMachineId) -and
            $MachineId -ne $LastObservedNonEmptyMachineId
        ) {
            $NodeChanges += 1
            Write-Warning (
                "$Service moved from Salad node $LastObservedNonEmptyMachineId to $MachineId; " +
                "global node changes=$NodeChanges/$($Profile.MaxNodeChanges)."
            )
            if ($NodeChanges -gt [int]$Profile.MaxNodeChanges) {
                throw (
                    "$Service exceeded the global node-change budget of " +
                    "$($Profile.MaxNodeChanges); refusing further reallocation churn."
                )
            }
        }
        $LastObservedNonEmptyMachineId = $MachineId
    }

    if ($ObservedInstance) {
        $IdentityChanged = $false
        if (
            -not [string]::IsNullOrWhiteSpace($CurrentMachineId) -and
            -not [string]::IsNullOrWhiteSpace($MachineId)
        ) {
            $IdentityChanged = $MachineId -ne $CurrentMachineId
        }
        elseif (-not [string]::IsNullOrWhiteSpace($CurrentInstanceId)) {
            $IdentityChanged = $InstanceId -ne $CurrentInstanceId
        }

        if ($IdentityChanged) {
            $AllocatingSince = $null
            $ImagePullSince = $null
            $ImagePullBaseline = $null
            $PostPullStartSince = $null
            $RunningNotReadySince = $null
        }

        $CurrentInstanceId = $InstanceId
        if (-not [string]::IsNullOrWhiteSpace($MachineId)) {
            $CurrentMachineId = $MachineId
        }
    }

    $PullRendered = if ($null -eq $PullingProgress) { "-" } else { $PullingProgress }
    $StatusLine = (
        (
            "{0} service={1} status={2} state={3} started={4} ready={5} " +
            "pulling_progress={6} machine={7} attached={8} node_changes={9}/{10}"
        ) -f
        (Get-Date -Format "HH:mm:ss"),
        $Service,
        $Status,
        $InstanceState,
        $Started,
        $Ready,
        $PullRendered,
        $MachineId,
        $Attached,
        $NodeChanges,
        $Profile.MaxNodeChanges
    )
    Write-Host $StatusLine

    $ContainerObservedRunning = $InstanceState -eq "running"
    $ContainerStarted = $Started -or $ContainerObservedRunning
    if ($ContainerStarted -and $null -eq $ContainerStartedSeconds) {
        $ContainerStartedSeconds = ((Get-Date) - $PrewarmStartedAt).TotalSeconds
    }
    if ($Ready -and $null -eq $ReadySeconds) {
        $ReadySeconds = ((Get-Date) - $PrewarmStartedAt).TotalSeconds
    }

    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.replicas -eq 1 -and
        [int]$Group.queue_autoscaler.min_replicas -eq $TargetMinReplicas -and
        $ObservedInstance -and
        $ContainerStarted -and
        $Ready
    ) {
        if ($Service -eq "fish_speech") {
            $ImagePullAndStartSeconds = $ContainerStartedSeconds - $AssignmentSeconds
            $BootstrapAfterStartSeconds = $ReadySeconds - $ContainerStartedSeconds
            Write-Host (
                "FISH_SPEECH_PREWARM_METRIC assignment_seconds={0:N1} " +
                "container_started_seconds={1:N1} image_pull_and_start_seconds={2:N1} " +
                "ready_seconds={3:N1} bootstrap_after_start_seconds={4:N1}" -f @(
                    [double]$AssignmentSeconds,
                    [double]$ContainerStartedSeconds,
                    [double]$ImagePullAndStartSeconds,
                    [double]$ReadySeconds,
                    [double]$BootstrapAfterStartSeconds
                )
            )
        }
        $HoldSuffix = if ($HoldReadyReplica) { " with min_replicas=1 pinned" } else { "" }
        Write-Host (
            "$Service prewarm complete: exactly one started ready replica$HoldSuffix, queue still empty."
        ) -ForegroundColor Green
        exit 0
    }

    if (-not $ObservedInstance) {
        continue
    }

    $PrePullPending = (
        -not $ContainerStarted -and
        $InstanceState -in @("allocating", "creating") -and
        ($null -eq $PullingProgress -or $PullingProgress -le 0.0)
    )
    if ($PrePullPending) {
        if ($null -eq $AllocatingSince) {
            $AllocatingSince = Get-Date
        }
        $Limit = if ($AllocatingReallocations -lt $Profile.MaxAllocatingReallocations) {
            [int]$Profile.AllocatingSeconds
        }
        else {
            [int]$Profile.FinalAllocatingSeconds
        }
        if (((Get-Date) - $AllocatingSince).TotalSeconds -ge $Limit) {
            if ($AllocatingReallocations -ge $Profile.MaxAllocatingReallocations) {
                throw (
                    "$Service could not make allocation/container-creation progress " +
                    "within the final ${Limit}s window."
                )
            }
            $AllocatingReallocations += 1
            Request-InstanceReallocation `
                -InstanceId $InstanceId `
                -Reason (
                    "Allocation/container creation made no image-pull progress " +
                    "for at least ${Limit}s"
                )
            $AllocatingSince = $null
            continue
        }
    }
    else {
        $AllocatingSince = $null
    }

    $FractionalImagePull = (
        $InstanceState -in @("downloading", "creating") -and
        $null -ne $PullingProgress -and
        $PullingProgress -gt 0.0 -and
        $PullingProgress -lt 1.0
    )
    if ($FractionalImagePull) {
        if ($null -eq $ImagePullSince) {
            $ImagePullSince = Get-Date
            $ImagePullBaseline = $PullingProgress
        }
        elseif (
            $PullingProgress -lt $ImagePullBaseline -or
            $PullingProgress -ge ($ImagePullBaseline + $ImagePullProgressThreshold)
        ) {
            $ImagePullSince = Get-Date
            $ImagePullBaseline = $PullingProgress
        }
        else {
            $Limit = if ($ImagePullReallocations -lt $Profile.MaxImagePullReallocations) {
                [int]$Profile.ImagePullStallSeconds
            }
            else {
                [int]$Profile.FinalImagePullStallSeconds
            }
            if (((Get-Date) - $ImagePullSince).TotalSeconds -ge $Limit) {
                if ($ImagePullReallocations -ge $Profile.MaxImagePullReallocations) {
                    throw "$Service image pull remained stalled during the final ${Limit}s window."
                }
                $ImagePullReallocations += 1
                Request-InstanceReallocation `
                    -InstanceId $InstanceId `
                    -Reason "Container image pull made less than 0.5% progress for ${Limit}s"
                $ImagePullSince = $null
                $ImagePullBaseline = $null
                continue
            }
        }
    }
    else {
        $ImagePullSince = $null
        $ImagePullBaseline = $null
    }

    $ImagePulledButNotStarted = (
        $null -ne $PullingProgress -and
        $PullingProgress -ge 1.0 -and
        -not $ContainerStarted
    )
    if ($ImagePulledButNotStarted) {
        if ($null -eq $PostPullStartSince) {
            $PostPullStartSince = Get-Date
        }
        $Limit = if (
            $PostPullStartReallocations -lt $Profile.MaxPostPullStartReallocations
        ) {
            [int]$Profile.PostPullStartSeconds
        }
        else {
            [int]$Profile.FinalPostPullStartSeconds
        }
        if (((Get-Date) - $PostPullStartSince).TotalSeconds -ge $Limit) {
            if (
                $PostPullStartReallocations -ge $Profile.MaxPostPullStartReallocations
            ) {
                throw (
                    "$Service image pull completed but the container never started during " +
                    "the final ${Limit}s window."
                )
            }
            $PostPullStartReallocations += 1
            Request-InstanceReallocation `
                -InstanceId $InstanceId `
                -Reason "Image pull completed but the container did not start within ${Limit}s"
            $PostPullStartSince = $null
            continue
        }
    }
    else {
        $PostPullStartSince = $null
    }

    if ($ContainerStarted -and $InstanceState -eq "running" -and -not $Ready) {
        if ($null -eq $RunningNotReadySince) {
            $RunningNotReadySince = Get-Date
        }
        $Limit = if (
            $RunningNotReadyReallocations -lt $Profile.MaxRunningNotReadyReallocations
        ) {
            [int]$Profile.RunningNotReadySeconds
        }
        else {
            [int]$Profile.FinalRunningNotReadySeconds
        }
        if (((Get-Date) - $RunningNotReadySince).TotalSeconds -ge $Limit) {
            if (
                $RunningNotReadyReallocations -ge $Profile.MaxRunningNotReadyReallocations
            ) {
                throw "$Service remained running but not ready during the final ${Limit}s window."
            }
            $RunningNotReadyReallocations += 1
            Request-InstanceReallocation `
                -InstanceId $InstanceId `
                -Reason "Model bootstrap remained running but not ready for ${Limit}s"
            $RunningNotReadySince = $null
            continue
        }
    }
    else {
        $RunningNotReadySince = $null
    }
}

throw "$Service did not become ready within the overall $TimeoutMinutes minute prewarm budget."
