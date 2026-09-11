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
        "User-Agent" = "ai-video-factory-queue-repair/1.2"
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

function Get-Queue {
    return Invoke-RestMethod -Uri $QueueUrl -Headers $Headers -TimeoutSec 30
}

function Test-QueueAttachment {
    param([Parameter(Mandatory)][object]$Queue)

    return @(
        @($Queue.container_groups) |
            Where-Object { [string]$_.name -eq $GroupName }
    ).Count -eq 1
}

function Test-GroupConfiguration {
    param([Parameter(Mandatory)][object]$Group)

    $Autoscaler = $Group.PSObject.Properties["queue_autoscaler"]
    if ($null -eq $Autoscaler -or $null -eq $Autoscaler.Value) {
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
        [int]$Group.queue_autoscaler.polling_period -eq [int]$Definition.autoscaler.polling_period
    )
}

function Wait-ForGroupDeleted {
    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    do {
        Start-Sleep -Seconds 3
        if ($null -eq (Try-Get-Group)) {
            return
        }
    }
    while ((Get-Date) -lt $Deadline)

    throw "Container group '$GroupName' was not deleted before timeout."
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

function Get-WorkerEnvironment {
    $Environment = @{}
    foreach ($Property in $Definition.environment.PSObject.Properties) {
        $Override = [Environment]::GetEnvironmentVariable($Property.Name)
        if ([string]::IsNullOrWhiteSpace($Override)) {
            $Environment[$Property.Name] = [string]$Property.Value
        }
        else {
            $Environment[$Property.Name] = $Override.Trim()
        }
    }

    $Required = @(
        $Document.stack.shared_required_environment |
            ForEach-Object { [string]$_ }
    )
    $Required += @(
        $Definition.required_environment |
            ForEach-Object { [string]$_ }
    )
    $SecretNames = @(
        "POSTGRES_DSN",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "HF_TOKEN"
    )
    foreach ($Name in @($Required | Select-Object -Unique)) {
        $Environment[$Name] = Get-Setting `
            -Name $Name `
            -Prompt $Name `
            -Secret:($SecretNames -contains $Name)
    }
    return $Environment
}

function New-Probe {
    param([Parameter(Mandatory)][object]$Probe)

    return @{
        http = @{
            headers = @()
            path = [string]$Probe.path
            port = $ContainerPort
            scheme = "http"
        }
        initial_delay_seconds = 0
        period_seconds = [int]$Probe.period_seconds
        failure_threshold = [int]$Probe.failure_threshold
        success_threshold = 1
        timeout_seconds = [int]$Probe.timeout_seconds
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

function New-QueueConnection {
    return @{
        path = $QueuePath
        port = $ContainerPort
        queue_name = $QueueName
    }
}

function New-CreateBody {
    param([Parameter(Mandatory)][object]$ExistingGroup)

    $GpuClasses = @(
        $ExistingGroup.container.resources.gpu_classes |
            ForEach-Object { [string]$_ }
    )
    if ($GpuClasses.Count -eq 0) {
        throw "Existing group '$GroupName' has no resolved GPU classes to preserve."
    }

    return @{
        name = $GroupName
        display_name = [string]$Definition.display_name
        autostart_policy = [bool]$Document.stack.autostart_policy
        replicas = 0
        restart_policy = [string]$Document.stack.restart_policy
        scheduled_scaling_enabled = $false
        container = @{
            image = [string]$ExistingGroup.container.image
            resources = @{
                cpu = [int]$Definition.resources.cpu
                memory = [int]$Definition.resources.memory
                gpu_classes = $GpuClasses
                shm_size = [int]$Definition.resources.shm_size
                storage_amount = [Int64]$Definition.resources.storage_amount
            }
            environment_variables = Get-WorkerEnvironment
            image_caching = $true
            priority = [string]$Definition.priority
        }
        startup_probe = New-Probe -Probe $Definition.probes.startup
        readiness_probe = New-Probe -Probe $Definition.probes.readiness
        liveness_probe = New-Probe -Probe $Definition.probes.liveness
        queue_connection = New-QueueConnection
        queue_autoscaler = New-QueueAutoscaler
    }
}

function Set-ZeroReplicas {
    param([Parameter(Mandatory)][object]$Group)

    if ([int]$Group.replicas -eq 0) {
        return $Group
    }

    Write-Host (
        "Normalizing stopped group {0} replicas from {1} to 0 before Prepare." -f `
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

$Queue = Get-Queue
if ([int]$Queue.current_queue_length -ne 0) {
    throw (
        "Queue '$QueueName' contains $($Queue.current_queue_length) job(s). " +
        "Cancel them before Prepare can change the container group."
    )
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
$Queue = Get-Queue
if ((Test-GroupConfiguration -Group $Group) -and (Test-QueueAttachment -Queue $Queue)) {
    Write-Host "$Service queue autoscaling verified; no repair needed." -ForegroundColor Green
    exit 0
}

$CreateBody = New-CreateBody -ExistingGroup $Group | ConvertTo-Json -Depth 20
Write-Warning (
    "Recreating stopped container group '$GroupName' so Salad can attach Job Queue autoscaling."
)
Invoke-RestMethod `
    -Method Delete `
    -Uri $GroupUrl `
    -Headers $Headers `
    -TimeoutSec 60 |
    Out-Null
Wait-ForGroupDeleted
Invoke-RestMethod `
    -Method Post `
    -Uri "$Base/containers" `
    -Headers $Headers `
    -ContentType "application/json" `
    -Body $CreateBody `
    -TimeoutSec 60 |
    Out-Null
$Group = Wait-ForGroupSettled

if (
    $null -eq $Group.PSObject.Properties["queue_autoscaler"] -or
    $null -eq $Group.queue_autoscaler
) {
    $PatchBody = @{
        queue_connection = New-QueueConnection
        queue_autoscaler = New-QueueAutoscaler
    } | ConvertTo-Json -Depth 10
    Invoke-RestMethod `
        -Method Patch `
        -Uri $GroupUrl `
        -Headers $Headers `
        -ContentType "application/merge-patch+json" `
        -Body $PatchBody `
        -TimeoutSec 60 |
        Out-Null
    $Group = Wait-ForGroupSettled
}

$Queue = Get-Queue
if (-not (Test-GroupConfiguration -Group $Group)) {
    throw "Salad did not persist the complete Job Queue autoscaling configuration."
}
if (-not (Test-QueueAttachment -Queue $Queue)) {
    throw "Salad did not attach '$GroupName' to Job Queue '$QueueName'."
}
if ([int]$Group.replicas -ne 0) {
    throw (
        "Queue repair unexpectedly left '$GroupName' at replicas=$([int]$Group.replicas)."
    )
}
Write-Host (
    "$Service Job Queue autoscaling repaired and verified; group remains at zero replicas."
) -ForegroundColor Green
