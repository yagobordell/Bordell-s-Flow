[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "ltx25", "realesrgan")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(10, 120)]
    [int]$TimeoutMinutes = 60,

    [switch]$HoldReadyReplica,

    [switch]$AdoptReadyReplica,

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
    realesrgan = @{
        AllocatingSeconds = 180
        FinalAllocatingSeconds = 480
        MaxAllocatingReallocations = 2
        ImagePullStallSeconds = 120
        FinalImagePullStallSeconds = 360
        MaxImagePullReallocations = 2
        PostPullStartSeconds = 120
        FinalPostPullStartSeconds = 300
        MaxPostPullStartReallocations = 1
        RunningNotReadySeconds = 240
        FinalRunningNotReadySeconds = 600
        MaxRunningNotReadyReallocations = 1
        MaxNodeChanges = 6
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

function Test-TransientSaladReadFailure {
    param([Parameter(Mandatory)][object]$ErrorRecord)

    $Exception = $ErrorRecord.Exception
    if (
        $Exception -is [System.Net.WebException] -and
        $Exception.Status -eq [System.Net.WebExceptionStatus]::Timeout
    ) {
        return $true
    }
    if ($Exception -is [System.TimeoutException]) {
        return $true
    }
    if (
        [string]$Exception.Message -match (
            '(?i)timed out|timeout|upstream connect error|disconnect/reset|' +
            'remote connection failure|server unavailable|gateway timeout'
        )
    ) {
        return $true
    }

    $StatusCode = Get-HttpStatusCode -ErrorRecord $ErrorRecord
    return (
        $StatusCode -eq 408 -or
        $StatusCode -eq 429 -or
        ($null -ne $StatusCode -and $StatusCode -ge 500 -and $StatusCode -le 599)
    )
}

function Invoke-SaladRead {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Operation,
        [ValidateRange(1, 60)][int]$TimeoutSeconds = 20,
        [ValidateRange(1, 10)][int]$MaxAttempts = 4,
        [datetime]$Deadline = [datetime]::MaxValue
    )

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt += 1) {
        $EffectiveTimeoutSeconds = $TimeoutSeconds
        if ($Deadline -ne [datetime]::MaxValue) {
            $SecondsRemaining = [Math]::Ceiling(($Deadline - (Get-Date)).TotalSeconds)
            if ($SecondsRemaining -le 0) {
                throw "Salad control-plane read '$Operation' exceeded its deadline."
            }
            $EffectiveTimeoutSeconds = [int][Math]::Max(
                1,
                [Math]::Min($TimeoutSeconds, $SecondsRemaining)
            )
        }

        try {
            return Invoke-RestMethod `
                -Uri $Uri `
                -Headers $Headers `
                -TimeoutSec $EffectiveTimeoutSeconds
        }
        catch {
            $Transient = Test-TransientSaladReadFailure -ErrorRecord $_
            if (-not $Transient -or $Attempt -ge $MaxAttempts) {
                throw
            }

            $DelaySeconds = [Math]::Min(10, 2 * $Attempt)
            if ($Deadline -ne [datetime]::MaxValue) {
                $SecondsRemaining = [Math]::Floor(($Deadline - (Get-Date)).TotalSeconds)
                if ($SecondsRemaining -le 0) {
                    throw "Salad control-plane read '$Operation' exceeded its deadline."
                }
                $DelaySeconds = [int][Math]::Min($DelaySeconds, $SecondsRemaining)
            }
            Write-Warning (
                "$Service Salad control-plane read '$Operation' failed transiently " +
                "(attempt $Attempt/$MaxAttempts): $($_.Exception.Message). " +
                "Retrying in ${DelaySeconds}s without reallocating the worker."
            )
            if ($DelaySeconds -gt 0) {
                Start-Sleep -Seconds $DelaySeconds
            }
        }
    }

    throw "Unreachable Salad read retry state for '$Operation'."
}

function Invoke-SaladMutation {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Operation,
        [Parameter(Mandatory)][string]$Method,
        [string]$ContentType = "",
        [string]$Body = "",
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 60,
        [ValidateRange(1, 10)][int]$MaxAttempts = 6
    )

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt += 1) {
        try {
            $Arguments = @{
                Method = $Method
                Uri = $Uri
                Headers = $Headers
                TimeoutSec = $TimeoutSeconds
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
            $Transient = Test-TransientSaladReadFailure -ErrorRecord $_
            if (-not $Transient -or $Attempt -ge $MaxAttempts) {
                throw
            }
            $DelaySeconds = [Math]::Min(15, 2 * $Attempt)
            Write-Warning (
                "$Service Salad control-plane mutation '$Operation' failed transiently " +
                "(attempt $Attempt/$MaxAttempts): $($_.Exception.Message). " +
                "Retrying in ${DelaySeconds}s."
            )
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    throw "Unreachable Salad mutation retry state for '$Operation'."
}

function Get-Group {
    return Invoke-SaladRead -Uri $GroupUrl -Operation "container group"
}

function Get-Queue {
    return Invoke-SaladRead -Uri $QueueUrl -Operation "queue"
}

function Get-RemoteQueueAutoscaler {
    param([Parameter(Mandatory)][object]$Group)

    $Property = $Group.PSObject.Properties["queue_autoscaler"]
    if ($null -eq $Property -or $null -eq $Property.Value) {
        return $null
    }
    return $Property.Value
}

function Test-RemoteAutoscalerMinReplicas {
    param(
        [Parameter(Mandatory)][object]$Group,
        [Parameter(Mandatory)][int]$ExpectedMinReplicas
    )

    $Autoscaler = Get-RemoteQueueAutoscaler -Group $Group
    if ($null -eq $Autoscaler) {
        return $false
    }
    return [int]$Autoscaler.min_replicas -eq $ExpectedMinReplicas
}

function New-ManifestAutoscaler {
    return @{
        min_replicas = [int]$Definition.autoscaler.min_replicas
        max_replicas = [int]$Definition.autoscaler.max_replicas
        desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
        polling_period = [int]$Definition.autoscaler.polling_period
        max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
        max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
    }
}

function Test-RemoteAutoscalerMatchesManifestExceptMinReplicas {
    param([Parameter(Mandatory)][object]$Group)

    $Autoscaler = Get-RemoteQueueAutoscaler -Group $Group
    if ($null -eq $Autoscaler) {
        return $false
    }
    return (
        [int]$Autoscaler.max_replicas -eq [int]$Definition.autoscaler.max_replicas -and
        [int]$Autoscaler.desired_queue_length -eq [int]$Definition.autoscaler.desired_queue_length -and
        [int]$Autoscaler.polling_period -eq [int]$Definition.autoscaler.polling_period -and
        [int]$Autoscaler.max_upscale_per_minute -eq `
            [int]$Definition.autoscaler.max_upscale_per_minute -and
        [int]$Autoscaler.max_downscale_per_minute -eq `
            [int]$Definition.autoscaler.max_downscale_per_minute
    )
}

function Repair-ResidualHeldAutoscaler {
    param([Parameter(Mandatory)][object]$Group)

    $Autoscaler = Get-RemoteQueueAutoscaler -Group $Group
    if ($null -eq $Autoscaler -or [int]$Autoscaler.min_replicas -eq 0) {
        return $Group
    }
    if (-not (Test-RemoteAutoscalerMatchesManifestExceptMinReplicas -Group $Group)) {
        throw (
            "Optimized prewarm refuses unexpected remote autoscaler drift; " +
            "only a residual warm-hold min_replicas override can be repaired automatically."
        )
    }

    Write-Warning (
        "$Service found a residual warm-hold autoscaler with " +
        "min_replicas=$([int]$Autoscaler.min_replicas) while the group is safely stopped at zero. " +
        "Restoring the manifest autoscaler before prewarm."
    )
    $Body = @{ queue_autoscaler = New-ManifestAutoscaler } | ConvertTo-Json -Depth 10 -Compress
    Invoke-SaladMutation `
        -Method "Patch" `
        -Uri $GroupUrl `
        -Operation "restore residual warm-hold autoscaler" `
        -ContentType "application/merge-patch+json" `
        -Body $Body |
        Out-Null

    $RepairDeadline = (Get-Date).AddMinutes(2)
    do {
        Start-Sleep -Seconds 3
        $Group = Get-Group
        if (
            -not [bool]$Group.pending_change -and
            (Test-RemoteAutoscalerMinReplicas -Group $Group -ExpectedMinReplicas 0) -and
            (Test-RemoteAutoscalerMatchesManifestExceptMinReplicas -Group $Group)
        ) {
            Write-Host "$Service residual warm-hold autoscaler restored to scale-to-zero." `
                -ForegroundColor Green
            return $Group
        }
    }
    while ((Get-Date) -lt $RepairDeadline)

    throw "Optimized prewarm could not restore the residual warm-hold autoscaler before timeout."
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
        $Response = Invoke-SaladRead `
            -Uri "$QueueUrl/jobs?page=$Page&page_size=25" `
            -Operation "queue jobs page $Page" `
            -TimeoutSeconds $RequestTimeoutSeconds `
            -MaxAttempts 6 `
            -Deadline $Deadline
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
        [ValidateRange(1, 300)][int]$VerificationSeconds = 180
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

function Resolve-QueueSummaryGrowth {
    param(
        [Parameter(Mandatory)][object]$Queue,
        [Parameter(Mandatory)][int]$VerifiedLength,
        [ValidateRange(1, 300)][int]$VerificationSeconds = 60
    )

    $ReportedLength = [int]$Queue.current_queue_length
    if ($ReportedLength -le $VerifiedLength) {
        return $VerifiedLength
    }

    Write-Warning (
        "$Service queue summary grew from $VerifiedLength to $ReportedLength during optimized " +
        "prewarm. Verifying enumerable jobs before treating the growth as new active work."
    )
    $Snapshot = Get-QueueJobSnapshot -Deadline (Get-Date).AddSeconds($VerificationSeconds)
    if (-not [bool]$Snapshot.complete) {
        throw (
            "Optimized prewarm could not verify queue-summary growth for '$QueueName' after " +
            "$([int]$Snapshot.pages) page(s); refusing to continue while queue state is ambiguous."
        )
    }

    $ActiveJobs = @($Snapshot.active_jobs)
    if ($ActiveJobs.Count -gt 0) {
        throw (
            "Optimized prewarm observed queue-summary growth with " +
            "$($ActiveJobs.Count) enumerable pending/running job(s); refusing to continue " +
            "while the worker is bootstrapping."
        )
    }

    Write-Warning (
        "$Service queue summary growth is stale: current_queue_length=$ReportedLength, but " +
        "enumeration found no pending or running jobs. Rebasing the verified stale summary."
    )
    return $ReportedLength
}

function Get-Instances {
    $Response = Invoke-SaladRead -Uri $InstancesUrl -Operation "container instances"
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
        [string]$MachineId = "",
        [Parameter(Mandatory)][string]$Reason
    )

    Write-Warning "$Service requesting Salad node reallocation: $Reason"
    for ($Attempt = 1; $Attempt -le 3; $Attempt += 1) {
        try {
            Invoke-SaladMutation `
                -Method "Post" `
                -Uri "$InstancesUrl/$InstanceId/reallocate" `
                -Operation "reallocate instance $InstanceId" `
                -TimeoutSeconds 60 `
                -MaxAttempts 1 |
                Out-Null
            return
        }
        catch {
            if (-not (Test-TransientSaladReadFailure -ErrorRecord $_) -or $Attempt -ge 3) {
                throw
            }

            Start-Sleep -Seconds 5
            $Instances = @(Get-Instances)
            $Matching = @($Instances | Where-Object { [string]$_.id -eq $InstanceId })
            if ($Matching.Count -eq 0) {
                Write-Warning (
                    "$Service reallocation response was lost, but instance $InstanceId is no longer " +
                    "present; treating the request as accepted."
                )
                return
            }

            $ObservedMachine = ""
            if ($Matching[0].PSObject.Properties.Name -contains "machine_id") {
                $ObservedMachine = [string]$Matching[0].machine_id
            }
            if (
                -not [string]::IsNullOrWhiteSpace($MachineId) -and
                -not [string]::IsNullOrWhiteSpace($ObservedMachine) -and
                $ObservedMachine -ne $MachineId
            ) {
                Write-Warning (
                    "$Service reallocation response was lost, but the instance moved from " +
                    "$MachineId to $ObservedMachine; treating the request as accepted."
                )
                return
            }

            $DelaySeconds = [Math]::Min(10, 2 * $Attempt)
            Write-Warning (
                "$Service reallocation request failed transiently and no node move is visible yet " +
                "(attempt $Attempt/3): $($_.Exception.Message). Retrying in ${DelaySeconds}s."
            )
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

function Test-QueueAttachment {
    param([Parameter(Mandatory)][object]$Queue)

    return @(
        @($Queue.container_groups) |
            Where-Object { [string]$_.name -eq $GroupName }
    ).Count -eq 1
}

function Test-QueueRuntimeReady {
    param(
        [Parameter(Mandatory)][object]$Queue,
        [Parameter(Mandatory)][string]$InstanceId
    )

    return Test-QueueAttachment -Queue $Queue
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
$LogsUrl = "https://api.salad.com/api/public/organizations/$Organization/log-entries"
$Headers = @{
    "Salad-Api-Key" = Get-SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-optimized-prewarm/1.4"
}

$Queue = Get-Queue
$null = Assert-QueueLogicallyEmpty -Queue $Queue
$VerifiedInitialQueueLength = [int]$Queue.current_queue_length

$Group = Get-Group
$Status = [string]$Group.current_state.status
if ([string]$Group.queue_connection.queue_name -ne $QueueName) {
    throw "Container group '$GroupName' is configured for an unexpected queue."
}

if (
    $AdoptReadyReplica -and
    $Status -eq "running" -and
    -not [bool]$Group.pending_change -and
    [int]$Group.replicas -eq 1
) {
    $HeldAutoscaler = Get-RemoteQueueAutoscaler -Group $Group
    if ($null -eq $HeldAutoscaler) {
        throw (
            "Optimized prewarm cannot adopt a shared ready replica because Salad did not expose " +
            "queue_autoscaler on container group '$GroupName'."
        )
    }
    if (
        [int]$HeldAutoscaler.min_replicas -ne 1 -or
        -not (Test-RemoteAutoscalerMatchesManifestExceptMinReplicas -Group $Group)
    ) {
        throw (
            "Optimized prewarm refuses to adopt a running replica unless it matches the " +
            "intentional min_replicas=1 shared-worker hold."
        )
    }

    $HeldInstances = @(Get-Instances)
    if ($HeldInstances.Count -ne 1) {
        throw (
            "Optimized prewarm expected exactly one instance while adopting the shared " +
            "ready replica; found $($HeldInstances.Count)."
        )
    }
    $HeldInstance = $HeldInstances[0]
    $HeldReady = (
        $HeldInstance.PSObject.Properties.Name -contains "ready" -and
        [bool]$HeldInstance.ready
    )
    $HeldStarted = (
        $HeldInstance.PSObject.Properties.Name -contains "started" -and
        [bool]$HeldInstance.started
    )
    if (-not $HeldStarted -or -not $HeldReady) {
        throw (
            "Optimized prewarm found the shared replica running but not started+ready; " +
            "refusing to submit new queue work."
        )
    }

    $Queue = Get-Queue
    $null = Assert-QueueLogicallyEmpty -Queue $Queue -VerificationSeconds 180
    $HeldInstanceId = ""
    if ($HeldInstance.PSObject.Properties.Name -contains "id") {
        $HeldInstanceId = [string]$HeldInstance.id
    }
    if (-not (Test-QueueRuntimeReady -Queue $Queue -InstanceId $HeldInstanceId)) {
        throw (
            "Optimized prewarm refuses to adopt the ready replica because no reliable " +
            "Job Queue association is available for '$GroupName'."
        )
    }
    Write-Host (
        "$Service prewarm adopted one already started+ready shared replica with " +
        "min_replicas=1 pinned; queue attached and still empty."
    ) -ForegroundColor Green
    exit 0
}

$AutostartProperty = $Definition.PSObject.Properties["autostart_policy"]
$AutostartEnabled = (
    $null -ne $AutostartProperty -and
    [bool]$AutostartProperty.Value
)
$AllowedInitialStatuses = @("stopped", "running")
if ($AutostartEnabled) {
    $AllowedInitialStatuses += "deploying"
}
if (
    $Status -notin $AllowedInitialStatuses -or
    [bool]$Group.pending_change -or
    [int]$Group.replicas -ne 0
) {
    throw (
        "Optimized prewarm requires '$GroupName' at replicas=0/pending=False in an allowed " +
        "lifecycle state; status=$Status autostart=$AutostartEnabled " +
        "replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
    )
}
$GroupAlreadyActive = $Status -in @("running", "deploying")
$RemoteAutoscaler = Get-RemoteQueueAutoscaler -Group $Group
$AutoscalerObservable = $null -ne $RemoteAutoscaler
if ($AutoscalerObservable) {
    $Group = Repair-ResidualHeldAutoscaler -Group $Group
    $RemoteAutoscaler = Get-RemoteQueueAutoscaler -Group $Group
    if ([int]$RemoteAutoscaler.min_replicas -ne 0) {
        throw "Optimized prewarm requires remote autoscaler min_replicas=0 after normalization."
    }
    if (
        $Service -in @("ideogram4", "fish_speech") -and
        [int]$RemoteAutoscaler.max_replicas -ne 1
    ) {
        throw "$Service optimized prewarm refuses a remote autoscaler with max_replicas != 1."
    }
}
elseif ($HoldReadyReplica) {
    throw (
        "Optimized prewarm cannot use -HoldReadyReplica because Salad did not expose " +
        "queue_autoscaler on container group '$GroupName'."
    )
}
else {
    Write-Host (
        "Salad did not expose queue_autoscaler for '$GroupName'; " +
        "continuing prewarm using explicit replicas=1 and the one-instance guard."
    ) -ForegroundColor Yellow
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
Invoke-SaladMutation `
    -Method "Patch" `
    -Uri $GroupUrl `
    -Operation "persist one-replica prewarm state" `
    -ContentType "application/merge-patch+json" `
    -Body ($PrewarmPatch | ConvertTo-Json -Depth 10 -Compress) `
    -TimeoutSeconds 60 |
    Out-Null

$PatchDeadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    $AutoscalerStateReady = (
        -not $AutoscalerObservable -or
        (Test-RemoteAutoscalerMinReplicas -Group $Group -ExpectedMinReplicas $TargetMinReplicas)
    )
    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.replicas -eq 1 -and
        $AutoscalerStateReady
    ) {
        break
    }
}
while ((Get-Date) -lt $PatchDeadline)
$AutoscalerStateReady = (
    -not $AutoscalerObservable -or
    (Test-RemoteAutoscalerMinReplicas -Group $Group -ExpectedMinReplicas $TargetMinReplicas)
)
if (
    [bool]$Group.pending_change -or
    [int]$Group.replicas -ne 1 -or
    -not $AutoscalerStateReady
) {
    throw "Salad did not persist the one-replica prewarm state safely."
}

if (-not $GroupAlreadyActive) {
    Invoke-SaladMutation `
        -Method "Post" `
        -Uri "$GroupUrl/start" `
        -Operation "start prewarmed container group" `
        -TimeoutSeconds 60 |
        Out-Null
}

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
$ReadyUnattachedSince = $null
$ReadyUnattachedTimeoutSeconds = 120
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
    $TransportReady = $Attached

    $ReportedQueueLength = [int]$Queue.current_queue_length
    if ($ReportedQueueLength -gt $VerifiedInitialQueueLength) {
        $VerifiedInitialQueueLength = Resolve-QueueSummaryGrowth `
            -Queue $Queue `
            -VerifiedLength $VerifiedInitialQueueLength `
            -VerificationSeconds 60
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

    if ($Ready -and -not $TransportReady) {
        $TransportReady = Test-QueueRuntimeReady -Queue $Queue -InstanceId $InstanceId
    }

    $PullRendered = if ($null -eq $PullingProgress) { "-" } else { $PullingProgress }
    $StatusLine = (
        (
            "{0} service={1} status={2} state={3} started={4} ready={5} " +
            "pulling_progress={6} machine={7} attached={8} transport_ready={9} node_changes={10}/{11}"
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
        $TransportReady,
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

    if ($Ready -and -not $TransportReady) {
        if ($null -eq $ReadyUnattachedSince) {
            $ReadyUnattachedSince = Get-Date
            Write-Warning (
                "$Service is ready but has not yet attached to the Job Queue for '$QueueName'; " +
                "waiting up to ${ReadyUnattachedTimeoutSeconds}s before failing safely."
            )
        }
        elseif (
            ((Get-Date) - $ReadyUnattachedSince).TotalSeconds -ge
            $ReadyUnattachedTimeoutSeconds
        ) {
            throw (
                "$Service reached ready state but Salad did not expose the Job Queue association for " +
                "'$GroupName' and queue '$QueueName' within " +
                "${ReadyUnattachedTimeoutSeconds}s."
            )
        }
    }
    else {
        $ReadyUnattachedSince = $null
    }

    $AutoscalerStateReady = (
        -not $AutoscalerObservable -or
        (Test-RemoteAutoscalerMinReplicas -Group $Group -ExpectedMinReplicas $TargetMinReplicas)
    )
    if (
        -not [bool]$Group.pending_change -and
        [int]$Group.replicas -eq 1 -and
        $AutoscalerStateReady -and
        $ObservedInstance -and
        $ContainerStarted -and
        $Ready -and
        $TransportReady
    ) {
        # The queue is dedicated to this service. Avoid repeatedly paginating historical
        # jobs while the image/model boots; re-establish the invariant once, immediately
        # before releasing the ready worker to the stage that will submit new work.
        $Queue = Get-Queue
        $null = Assert-QueueLogicallyEmpty -Queue $Queue -VerificationSeconds 180

        if ($Service -eq "fish_speech") {
            $ImagePullAndStartSeconds = $ContainerStartedSeconds - $AssignmentSeconds
            $BootstrapAfterStartSeconds = $ReadySeconds - $ContainerStartedSeconds
            Write-Host (
                (
                    "FISH_SPEECH_PREWARM_METRIC assignment_seconds={0:N1} " +
                    "container_started_seconds={1:N1} image_pull_and_start_seconds={2:N1} " +
                    "ready_seconds={3:N1} bootstrap_after_start_seconds={4:N1}"
                ) -f @(
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
            "$Service prewarm complete: exactly one started ready replica$HoldSuffix, " +
            "queue attached and still empty."
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
                -MachineId $MachineId `
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
                    -MachineId $MachineId `
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
                -MachineId $MachineId `
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
                -MachineId $MachineId `
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
