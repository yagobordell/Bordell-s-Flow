[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "ltx25")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(1, 120)]
    [int]$TimeoutMinutes = 90,

    [ValidateRange(1, 60)]
    [int]$AllocatingTimeoutMinutes = 10,

    [ValidateRange(1, 10)]
    [int]$MaxDownloadReallocations = 3,

    [ValidateRange(1, 60)]
    [int]$DownloadStallTimeoutMinutes = 10,

    [ValidateRange(1, 60)]
    [int]$RunningNotReadyTimeoutMinutes = 20,

    [ValidateRange(1, 10)]
    [int]$MaxRunningNotReadyReallocations = 2,

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
        return $true
    }
    return [int]$Autoscaler.min_replicas -eq $ExpectedMinReplicas
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
    param([Parameter(Mandatory)][string]$InstanceId)

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
$RemoteAutoscaler = Get-RemoteQueueAutoscaler -Group $Group
if ($null -ne $RemoteAutoscaler -and [int]$RemoteAutoscaler.min_replicas -ne 0) {
    throw (
        "Protected smoke bootstrap requires the remote autoscaler to remain at " +
        "min_replicas=0 before requesting a manual replica."
    )
}
if ($null -eq $RemoteAutoscaler) {
    Write-Host (
        "Salad did not expose queue_autoscaler for '$GroupName'; " +
        "continuing protected smoke with explicit replicas=1."
    ) -ForegroundColor Yellow
}

$Body = @{ replicas = 1 } | ConvertTo-Json -Depth 10
Write-Host (
    "$Service protected smoke temporarily setting replicas=1 while keeping " +
    "queue_autoscaler.min_replicas=0."
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
        [int]$Group.replicas -eq 1 -and
        (Test-RemoteAutoscalerMinReplicas -Group $Group -ExpectedMinReplicas 0)
    ) {
        break
    }
}
while ((Get-Date) -lt $PatchDeadline)
if (
    [int]$Group.replicas -ne 1 -or
    [bool]$Group.pending_change -or
    -not (Test-RemoteAutoscalerMinReplicas -Group $Group -ExpectedMinReplicas 0)
) {
    throw (
        "Salad did not persist protected smoke replicas=1 with " +
        "queue_autoscaler.min_replicas=0 before Start."
    )
}

Invoke-RestMethod `
    -Method Post `
    -Uri "$GroupUrl/start" `
    -Headers $Headers `
    -TimeoutSec 60 |
    Out-Null

$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$StartedBootstrapDeadlineSet = $false
$DownloadReallocations = 0
$DownloadProgressThreshold = 0.005
$DownloadProgressBaseline = $null
$DownloadProgressSince = $null
$DownloadProgressInstanceId = ""
$ReallocationPending = $false
$ReallocatedMachineId = ""
$AllocatingSince = $null
$AllocatingInstanceId = ""
$RunningNotReadySince = $null
$RunningNotReadyInstanceId = ""
$RunningNotReadyReallocations = 0
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    $Queue = Get-Queue
    $Instances = @(Get-Instances)
    $Status = [string]$Group.current_state.status
    $Attached = Test-QueueAttachment -Queue $Queue
    $StartedInstances = @($Instances | Where-Object { [bool]$_.started })

    if (-not $StartedBootstrapDeadlineSet -and $StartedInstances.Count -eq 1) {
        $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
        $StartedBootstrapDeadlineSet = $true
        Write-Host (
            "{0} service={1} started bootstrap timeout window={2}m" -f
            (Get-Date -Format "HH:mm:ss"),
            $Service,
            $TimeoutMinutes
        )
    }

    $InstanceState = "-"
    $PullingProgress = "-"
    $PullingProgressValue = $null
    $InstanceId = ""
    $MachineId = "-"
    $Ready = $false
    if ($Instances.Count -eq 1) {
        $Instance = $Instances[0]
        if ($Instance.PSObject.Properties.Name -contains "id") {
            $InstanceId = [string]$Instance.id
        }
        if ($Instance.PSObject.Properties.Name -contains "state") {
            $InstanceState = [string]$Instance.state
        }
        if ($Instance.PSObject.Properties.Name -contains "machine_id") {
            $MachineId = [string]$Instance.machine_id
        }
        if ($Instance.PSObject.Properties.Name -contains "pulling_progress") {
            $PullingProgress = [string]$Instance.pulling_progress
            $PullingProgressValue = [double]$Instance.pulling_progress
        }
        if ($Instance.PSObject.Properties.Name -contains "ready") {
            $Ready = [bool]$Instance.ready
        }
    }

    $Message = (
        "{0} service={1} status={2} replicas={3} instances={4} started={5} " +
        "state={6} pulling_progress={7} ready={8} attached={9} pending={10}"
    ) -f (
        (Get-Date -Format "HH:mm:ss"),
        $Service,
        $Status,
        [int]$Group.replicas,
        $Instances.Count,
        $StartedInstances.Count,
        $InstanceState,
        $PullingProgress,
        $Ready,
        $Attached,
        [bool]$Group.pending_change
    )
    Write-Host $Message

    if (
        $Service -eq "ltx25" -and
        $Instances.Count -eq 1 -and
        $InstanceState -eq "allocating"
    ) {
        if (
            $null -eq $AllocatingSince -or
            $InstanceId -ne $AllocatingInstanceId
        ) {
            $AllocatingSince = Get-Date
            $AllocatingInstanceId = $InstanceId
            Write-Host (
                "{0} service={1} allocating watchdog started limit={2}m" -f
                (Get-Date -Format "HH:mm:ss"),
                $Service,
                $AllocatingTimeoutMinutes
            ) -ForegroundColor Cyan
        }

        $AllocatingElapsed = (Get-Date) - $AllocatingSince
        if ($AllocatingElapsed.TotalMinutes -ge $AllocatingTimeoutMinutes) {
            throw (
                "LTX instance remained in allocating for at least " +
                "$AllocatingTimeoutMinutes minute(s); aborting protected bootstrap."
            )
        }
    }
    else {
        $AllocatingSince = $null
        $AllocatingInstanceId = ""
    }

    if (
        $ReallocationPending -and
        $Instances.Count -eq 1 -and
        $MachineId -ne "-" -and
        $MachineId -ne $ReallocatedMachineId
    ) {
        Write-Host (
            "{0} service={1} reallocation completed on a different Salad node" -f
            (Get-Date -Format "HH:mm:ss"),
            $Service
        ) -ForegroundColor Cyan
        $ReallocationPending = $false
    }

    $RunningNotReady = (
        $Service -eq "ltx25" -and
        $Instances.Count -eq 1 -and
        $StartedInstances.Count -eq 1 -and
        $InstanceState -eq "running" -and
        -not $Ready
    )
    if ($RunningNotReady) {
        if (
            $null -eq $RunningNotReadySince -or
            $InstanceId -ne $RunningNotReadyInstanceId
        ) {
            $RunningNotReadySince = Get-Date
            $RunningNotReadyInstanceId = $InstanceId
            Write-Host (
                "{0} service={1} running-not-ready watchdog started limit={2}m" -f
                (Get-Date -Format "HH:mm:ss"),
                $Service,
                $RunningNotReadyTimeoutMinutes
            ) -ForegroundColor Cyan
        }

        $RunningNotReadyElapsed = (Get-Date) - $RunningNotReadySince
        if (
            $RunningNotReadyElapsed.TotalMinutes -ge $RunningNotReadyTimeoutMinutes -and
            -not $ReallocationPending
        ) {
            if (
                $RunningNotReadyReallocations -ge $MaxRunningNotReadyReallocations
            ) {
                throw (
                    "LTX model bootstrap remained running but not ready after " +
                    "$MaxRunningNotReadyReallocations Salad node reallocations."
                )
            }

            if ([string]::IsNullOrWhiteSpace($InstanceId)) {
                throw (
                    "Cannot reallocate stalled LTX model bootstrap because " +
                    "instance id is missing."
                )
            }

            $RunningNotReadyReallocations += 1
            $ReallocationPending = $true
            $ReallocatedMachineId = $MachineId

            Write-Warning (
                "$Service remained running but not ready for at least " +
                "$RunningNotReadyTimeoutMinutes minute(s); reallocating to " +
                "another Salad node " +
                "($RunningNotReadyReallocations/" +
                "$MaxRunningNotReadyReallocations)."
            )

            Request-InstanceReallocation -InstanceId $InstanceId

            $RunningNotReadySince = $null
            $RunningNotReadyInstanceId = ""
            $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
            $StartedBootstrapDeadlineSet = $false
            continue
        }
    }
    else {
        $RunningNotReadySince = $null
        $RunningNotReadyInstanceId = ""
    }

    $FractionalDownload = (
        $Service -eq "ltx25" -and
        $Instances.Count -eq 1 -and
        -not $ReallocationPending -and
        $InstanceState -eq "downloading" -and
        $null -ne $PullingProgressValue -and
        $PullingProgressValue -gt 0.0 -and
        $PullingProgressValue -lt 1.0
    )
    if ($FractionalDownload) {
        if (
            $null -eq $DownloadProgressSince -or
            $InstanceId -ne $DownloadProgressInstanceId
        ) {
            $DownloadProgressBaseline = $PullingProgressValue
            $DownloadProgressSince = Get-Date
            $DownloadProgressInstanceId = $InstanceId

            Write-Host (
                "{0} service={1} image-pull watchdog started progress={2} " +
                "stall_limit={3}m threshold={4}" -f
                (Get-Date -Format "HH:mm:ss"),
                $Service,
                $PullingProgress,
                $DownloadStallTimeoutMinutes,
                $DownloadProgressThreshold
            ) -ForegroundColor Cyan
        }
        elseif (
            $PullingProgressValue -lt $DownloadProgressBaseline -or
            $PullingProgressValue -ge (
                $DownloadProgressBaseline + $DownloadProgressThreshold
            )
        ) {
            $DownloadProgressBaseline = $PullingProgressValue
            $DownloadProgressSince = Get-Date
        }
        else {
            $DownloadStallElapsed = (Get-Date) - $DownloadProgressSince
            if ($DownloadStallElapsed.TotalMinutes -ge $DownloadStallTimeoutMinutes) {
                if ($DownloadReallocations -ge $MaxDownloadReallocations) {
                    throw (
                        "LTX image pull remained stalled after " +
                        "$MaxDownloadReallocations Salad node reallocations."
                    )
                }

                if ([string]::IsNullOrWhiteSpace($InstanceId)) {
                    throw (
                        "Cannot reallocate stalled LTX image pull because " +
                        "instance id is missing."
                    )
                }

                $DownloadReallocations += 1
                $ReallocationPending = $true
                $ReallocatedMachineId = $MachineId

                Write-Warning (
                    "$Service image pull made less than " +
                    "$DownloadProgressThreshold progress for at least " +
                    "$DownloadStallTimeoutMinutes minute(s); reallocating " +
                    "to another Salad node " +
                    "($DownloadReallocations/$MaxDownloadReallocations)."
                )

                Request-InstanceReallocation -InstanceId $InstanceId

                $DownloadProgressBaseline = $null
                $DownloadProgressSince = $null
                $DownloadProgressInstanceId = ""
                $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
                $StartedBootstrapDeadlineSet = $false
                continue
            }
        }
    }
    else {
        $DownloadProgressBaseline = $null
        $DownloadProgressSince = $null
        $DownloadProgressInstanceId = ""
    }

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
        [int]$Group.replicas -eq 1 -and
        (Test-RemoteAutoscalerMinReplicas -Group $Group -ExpectedMinReplicas 0) -and
        $Instances.Count -eq 1 -and
        $StartedInstances.Count -eq 1 -and
        $Ready
    ) {
        Write-Warning (
            "$Service protected bootstrap verified one started ready instance; " +
            "queue attachment observation=$Attached; " +
            "the caller must stop and normalize replicas=0 in a finally block."
        )
        exit 0
    }
}
while ((Get-Date) -lt $Deadline)

throw (
    "Container group did not expose one started ready bootstrap instance before timeout."
)
