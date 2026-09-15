[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "ideogram4", "ltx25")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(1, 30)]
    [int]$TimeoutMinutes = 5,

    [switch]$AllowMissing,

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
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name))) {
            [Environment]::SetEnvironmentVariable($Name, $Value)
        }
    }
}

function Get-Setting {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Prompt,
        [switch]$Secret
    )

    $Value = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value.Trim()
    }
    if ($NonInteractive) {
        throw "$Name is missing and -NonInteractive was requested."
    }
    if ($Secret) {
        $SecureValue = Read-Host $Prompt -AsSecureString
        $Credential = [PSCredential]::new("salad-queue-repair", $SecureValue)
        $Value = $Credential.GetNetworkCredential().Password
    }
    else {
        $Value = Read-Host $Prompt
    }
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is empty."
    }
    $Value = $Value.Trim()
    [Environment]::SetEnvironmentVariable($Name, $Value)
    return $Value
}

function Get-Headers {
    return @{
        "Salad-Api-Key" = Get-Setting -Name "SALAD_API_KEY" -Prompt "Salad API key" -Secret
        "Accept" = "application/json"
        "User-Agent" = "ai-video-factory-queue-repair/1.8"
    }
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

function Try-Get-Group {
    try {
        return Invoke-RestMethod -Uri $GroupUrl -Headers $Headers -TimeoutSec 30
    }
    catch {
        if ((Get-HttpStatusCode -ErrorRecord $_) -eq 404) {
            return $null
        }
        throw
    }
}

function Try-Get-Queue {
    try {
        return Invoke-RestMethod -Uri $QueueUrl -Headers $Headers -TimeoutSec 30
    }
    catch {
        if ((Get-HttpStatusCode -ErrorRecord $_) -eq 404) {
            return $null
        }
        throw
    }
}

function Get-Queue {
    $Queue = Try-Get-Queue
    if ($null -eq $Queue) {
        throw "Job queue '$QueueName' is missing. Run Prepare first."
    }
    return $Queue
}

function Get-ActiveQueueJobs {
    $ActiveJobs = @()
    $Page = 1
    $PageSize = 25

    while ($true) {
        if ($Page -gt 100) {
            throw "Job queue '$QueueName' exceeded the 100-page safety limit while listing jobs."
        }

        $JobsUrl = "$QueueUrl/jobs?page=$Page&page_size=$PageSize"
        $Response = Invoke-RestMethod -Uri $JobsUrl -Headers $Headers -TimeoutSec 30
        $Items = @($Response.items)
        $ActiveJobs += @(
            $Items |
                Where-Object { [string]$_.status -in @("pending", "running") }
        )

        if ($Items.Count -lt $PageSize) {
            break
        }
        $Page += 1
    }

    return @($ActiveJobs)
}

function Write-ActiveQueueJobs {
    param([Parameter(Mandatory)][object[]]$Jobs)

    foreach ($Job in $Jobs) {
        $ApplicationJobId = ""
        if ($null -ne $Job.input -and $null -ne $Job.input.PSObject.Properties["job_id"]) {
            $ApplicationJobId = [string]$Job.input.job_id
        }
        Write-Warning (
            "active transport={0} status={1} application={2}" -f `
            [string]$Job.id,
            [string]$Job.status,
            $ApplicationJobId
        )
    }
}

function Test-QueueAttachment {
    param([Parameter(Mandatory)][object]$Queue)

    return @(
        @($Queue.container_groups) |
            Where-Object { [string]$_.name -eq $GroupName }
    ).Count -eq 1
}

function New-QueueConnection {
    return @{
        path = $QueuePath
        port = $ContainerPort
        queue_name = $QueueName
    }
}

function New-QueueAutoscaler {
    return @{
        min_replicas = [int]$Definition.autoscaler.min_replicas
        max_replicas = [int]$Definition.autoscaler.max_replicas
        desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
        polling_period = [int]$Definition.autoscaler.polling_period
        max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
        max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
    }
}

function Test-GroupConfiguration {
    param([Parameter(Mandatory)][object]$Group)

    $Connection = $Group.PSObject.Properties["queue_connection"]
    $Autoscaler = $Group.PSObject.Properties["queue_autoscaler"]
    if (
        $null -eq $Connection -or $null -eq $Connection.Value -or
        $null -eq $Autoscaler -or $null -eq $Autoscaler.Value
    ) {
        return $false
    }

    $Networking = $Group.PSObject.Properties["networking"]
    if ($null -ne $Networking -and $null -ne $Networking.Value) {
        return $false
    }

    return (
        [string]$Group.queue_connection.queue_name -eq $QueueName -and
        [string]$Group.queue_connection.path -eq $QueuePath -and
        [int]$Group.queue_connection.port -eq $ContainerPort -and
        [int]$Group.queue_autoscaler.min_replicas -eq [int]$Definition.autoscaler.min_replicas -and
        [int]$Group.queue_autoscaler.max_replicas -eq [int]$Definition.autoscaler.max_replicas -and
        [int]$Group.queue_autoscaler.desired_queue_length -eq [int]$Definition.autoscaler.desired_queue_length -and
        [int]$Group.queue_autoscaler.polling_period -eq [int]$Definition.autoscaler.polling_period -and
        [int]$Group.queue_autoscaler.max_upscale_per_minute -eq `
            [int]$Definition.autoscaler.max_upscale_per_minute -and
        [int]$Group.queue_autoscaler.max_downscale_per_minute -eq `
            [int]$Definition.autoscaler.max_downscale_per_minute
    )
}

function Wait-ForGroupSettled {
    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    do {
        Start-Sleep -Seconds 5
        $Group = Try-Get-Group
        if ($null -ne $Group -and -not [bool]$Group.pending_change) {
            return $Group
        }
    }
    while ((Get-Date) -lt $Deadline)

    throw "Container group '$GroupName' did not settle before timeout."
}

function Set-ZeroReplicas {
    param([Parameter(Mandatory)][object]$Group)

    if ([int]$Group.replicas -eq 0) {
        return $Group
    }

    Write-Host (
        "Normalizing group {0} replicas from {1} to 0." -f `
        $GroupName,
        [int]$Group.replicas
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

    $Updated = Wait-ForGroupSettled
    if ([int]$Updated.replicas -ne 0) {
        throw (
            "Salad did not normalize '$GroupName' to zero replicas; current replicas={0}." -f `
            [int]$Updated.replicas
        )
    }
    return $Updated
}

function Repair-GroupConfiguration {
    param([Parameter(Mandatory)][object]$Group)

    $Networking = $Group.PSObject.Properties["networking"]
    if ($null -ne $Networking -and $null -ne $Networking.Value) {
        throw (
            "Container group '$GroupName' has networking enabled, which Salad does not allow with " +
            "queue_connection. Increment services.$Service.group_name in deploy/salad/services.json " +
            "and run Prepare again."
        )
    }

    Write-Host (
        "Repairing Job Queue autoscaling in place for stopped group '$GroupName'..."
    ) -ForegroundColor Cyan
    $Body = @{
        queue_connection = New-QueueConnection
        queue_autoscaler = New-QueueAutoscaler
        replicas = 0
    } | ConvertTo-Json -Depth 10

    Invoke-RestMethod `
        -Method Patch `
        -Uri $GroupUrl `
        -Headers $Headers `
        -ContentType "application/merge-patch+json" `
        -Body $Body `
        -TimeoutSec 60 |
        Out-Null

    $Updated = Wait-ForGroupSettled
    if ([int]$Updated.replicas -ne 0) {
        Write-Warning (
            "Salad raised '$GroupName' to replicas=$([int]$Updated.replicas) while applying " +
            "the Job Queue autoscaler. Forcing replicas back to zero now."
        )
        $Updated = Set-ZeroReplicas -Group $Updated
    }
    if (-not (Test-GroupConfiguration -Group $Updated)) {
        throw "Salad did not persist the complete Job Queue autoscaling configuration after PATCH."
    }
    return $Updated
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
$QueueName = [string]$Definition.queue_name
$QueuePath = [string]$Document.stack.queue_path
$ContainerPort = [int]$Document.stack.container_port
$Base = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$GroupUrl = "$Base/containers/$GroupName"
$QueueUrl = "$Base/queues/$QueueName"
$Headers = Get-Headers

$Queue = Try-Get-Queue
if ($null -eq $Queue) {
    if ($AllowMissing) {
        Write-Host "$Service has no existing job queue; preflight repair not needed." `
            -ForegroundColor Green
        exit 0
    }
    throw "Job queue '$QueueName' is missing. Run Prepare first."
}

$ActiveJobs = @(Get-ActiveQueueJobs)
if ($ActiveJobs.Count -ne 0) {
    Write-ActiveQueueJobs -Jobs $ActiveJobs
    throw (
        "Queue '$QueueName' contains $($ActiveJobs.Count) active job(s) " +
        "(current_queue_length=$($Queue.current_queue_length)). Cancel pending jobs and allow " +
        "running jobs to finish before changing the container group."
    )
}
if ([int]$Queue.current_queue_length -ne 0) {
    Write-Warning (
        "Queue '$QueueName' reports current_queue_length=$($Queue.current_queue_length), " +
        "but exhaustive pagination found no pending/running jobs. Terminal queue history will " +
        "not block Prepare."
    )
}
else {
    Write-Host "Queue '$QueueName' has no pending/running jobs." -ForegroundColor Green
}

$Group = Try-Get-Group
if ($null -eq $Group) {
    if ($AllowMissing) {
        Write-Host "$Service has no existing container group; preflight repair not needed." `
            -ForegroundColor Green
        exit 0
    }
    throw "Container group '$GroupName' is missing. Run Prepare first."
}

if ([string]$Group.current_state.status -ne "stopped") {
    throw (
        "Queue repair requires '$GroupName' to be stopped. " +
        "Current status=$([string]$Group.current_state.status)."
    )
}

$Group = Set-ZeroReplicas -Group $Group
if (-not (Test-GroupConfiguration -Group $Group)) {
    $Group = Repair-GroupConfiguration -Group $Group
}

$Queue = Get-Queue
if (Test-QueueAttachment -Queue $Queue) {
    Write-Host "$Service queue autoscaling and queue listing verified." -ForegroundColor Green
}
else {
    Write-Warning (
        "$Service queue autoscaling is configured on the stopped container group, " +
        "but Salad does not list stopped groups in queue.container_groups reliably. " +
        "Runtime attachment will be validated after Start/Smoke."
    )
}

Write-Host (
    "$Service Job Queue autoscaling configuration verified; group remains at zero replicas."
) -ForegroundColor Green
