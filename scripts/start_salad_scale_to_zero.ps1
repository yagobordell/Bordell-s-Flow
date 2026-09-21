[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "flux2_klein", "ltx25", "realesrgan")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(1, 10)]
    [int]$TimeoutMinutes = 2,

    [switch]$AllowBootstrapReplica,

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
    $Credential = [PSCredential]::new("salad-start", $SecureValue)
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
                "$Service scale-to-zero operation '$Operation' failed transiently " +
                "(attempt $Attempt/$MaxAttempts): $($_.Exception.Message). " +
                "Retrying in ${DelaySeconds}s."
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
    return Invoke-SaladRequest -Uri $QueueUrl -Operation "read queue summary"
}

function Test-QueueAttachment {
    param([Parameter(Mandatory)][object]$Queue)

    return @(
        @($Queue.container_groups) |
            Where-Object { [string]$_.name -eq $GroupName }
    ).Count -eq 1
}

function Test-GroupQueueConfiguration {
    param([Parameter(Mandatory)][object]$Group)

    $Connection = $Group.PSObject.Properties["queue_connection"]
    if ($null -eq $Connection -or $null -eq $Connection.Value) {
        return $false
    }

    $Networking = $Group.PSObject.Properties["networking"]
    if ($null -ne $Networking -and $null -ne $Networking.Value) {
        return $false
    }

    $ConnectionMatches = (
        [string]$Connection.Value.queue_name -eq $QueueName -and
        [string]$Connection.Value.path -eq [string]$Document.stack.queue_path -and
        [int]$Connection.Value.port -eq [int]$Document.stack.container_port
    )
    if (-not $ConnectionMatches) {
        return $false
    }

    $Autoscaler = $Group.PSObject.Properties["queue_autoscaler"]
    if ($null -eq $Autoscaler -or $null -eq $Autoscaler.Value) {
        return $true
    }

    return (
        [int]$Autoscaler.Value.min_replicas -eq [int]$Definition.autoscaler.min_replicas -and
        [int]$Autoscaler.Value.max_replicas -eq [int]$Definition.autoscaler.max_replicas -and
        [int]$Autoscaler.Value.desired_queue_length -eq `
            [int]$Definition.autoscaler.desired_queue_length -and
        [int]$Autoscaler.Value.polling_period -eq `
            [int]$Definition.autoscaler.polling_period -and
        [int]$Autoscaler.Value.max_upscale_per_minute -eq `
            [int]$Definition.autoscaler.max_upscale_per_minute -and
        [int]$Autoscaler.Value.max_downscale_per_minute -eq `
            [int]$Definition.autoscaler.max_downscale_per_minute
    )
}

function Assert-GroupQueueConfiguration {
    param([Parameter(Mandatory)][object]$Group)

    if (Test-GroupQueueConfiguration -Group $Group) {
        return
    }

    throw (
        "Container group '$GroupName' Job Queue configuration does not match " +
        "deploy/salad/services.json. Keep the group stopped and run " +
        "scripts/repair_salad_queue_attachment.ps1 -Service $Service before Start."
    )
}

function Test-ScaleToZeroActive {
    param([Parameter(Mandatory)][object]$Group)

    $Status = [string]$Group.current_state.status
    if ($AllowBootstrapReplica) {
        return (
            -not [bool]$Group.pending_change -and
            [int]$Group.replicas -eq 1 -and
            $Status -in @("deploying", "running")
        )
    }

    if ($Status -eq "running" -and -not [bool]$Group.pending_change) {
        return $true
    }

    if (
        [int]$Definition.autoscaler.min_replicas -ne 0 -or
        [bool]$Group.pending_change -or
        $Status -ne "deploying"
    ) {
        return $false
    }

    return [int]$Group.replicas -eq 0
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
    throw "Scale-to-zero starter requires min_replicas=0 for '$Service'."
}

$Organization = [string]$Document.stack.organization
$Project = [string]$Document.stack.project
$GroupName = [string]$Definition.group_name
$QueueName = [string]$Definition.queue_name
$BaseUrl = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$GroupUrl = "$BaseUrl/containers/$GroupName"
$QueueUrl = "$BaseUrl/queues/$QueueName"
$Headers = @{
    "Salad-Api-Key" = Get-SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-scale-to-zero-starter/1.2"
}

if ($AllowBootstrapReplica) {
    $Queue = Get-Queue
    if ([int]$Queue.current_queue_length -ne 0) {
        throw (
            "Protected smoke requires an empty queue before bootstrap; " +
            "'$QueueName' contains $([int]$Queue.current_queue_length) job(s)."
        )
    }
}

$Group = Get-Group
$RemoteAutoscalerProperty = $Group.PSObject.Properties["queue_autoscaler"]
if ($null -eq $RemoteAutoscalerProperty -or $null -eq $RemoteAutoscalerProperty.Value) {
    Write-Warning (
        "Salad did not expose queue_autoscaler for '$GroupName'; " +
        "validating queue connection and explicit replica state only."
    )
}
Assert-GroupQueueConfiguration -Group $Group
if (-not $AllowBootstrapReplica -and (Test-ScaleToZeroActive -Group $Group)) {
    Write-Host (
        "{0} scale-to-zero active: status={1} replicas={2} pending={3}" -f
        $Service,
        [string]$Group.current_state.status,
        [int]$Group.replicas,
        [bool]$Group.pending_change
    ) -ForegroundColor Green
    exit 0
}

if ([string]$Group.current_state.status -eq "failed") {
    throw "Container group '$GroupName' is failed before Start."
}

if ([string]$Group.current_state.status -eq "stopped") {
    Invoke-SaladRequest `
        -Method "Post" `
        -Uri "$GroupUrl/start" `
        -Operation "start container group" `
        -TimeoutSec 60 |
        Out-Null
}

$BootstrapRequested = $false
$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 5
    $Group = Get-Group
    Assert-GroupQueueConfiguration -Group $Group
    $Status = [string]$Group.current_state.status
    Write-Host (
        "{0} service={1} status={2} replicas={3} pending={4} description={5}" -f
        (Get-Date -Format "HH:mm:ss"),
        $Service,
        $Status,
        [int]$Group.replicas,
        [bool]$Group.pending_change,
        [string]$Group.current_state.description
    )

    if ($Status -eq "failed") {
        throw "Container group '$GroupName' entered failed state during Start."
    }

    if ($AllowBootstrapReplica) {
        if ([int]$Group.replicas -gt 1) {
            throw (
                "Protected smoke refuses to continue with more than one replica; " +
                "'$GroupName' reports $([int]$Group.replicas)."
            )
        }

        if (
            -not $BootstrapRequested -and
            -not [bool]$Group.pending_change -and
            [int]$Group.replicas -eq 0 -and
            $Status -in @("deploying", "running")
        ) {
            $Body = @{ replicas = 1 } | ConvertTo-Json
            Write-Host (
                "$Service protected smoke requesting exactly one bootstrap replica."
            ) -ForegroundColor Cyan
            Invoke-SaladRequest `
                -Method "Patch" `
                -Uri $GroupUrl `
                -Operation "request protected bootstrap replica" `
                -ContentType "application/merge-patch+json" `
                -Body $Body `
                -TimeoutSec 60 |
                Out-Null
            $BootstrapRequested = $true
            continue
        }

        if (
            $BootstrapRequested -and
            -not [bool]$Group.pending_change -and
            [int]$Group.replicas -eq 0
        ) {
            throw (
                "Protected smoke bootstrap replica returned to zero before queue attachment " +
                "was verified for '$GroupName'."
            )
        }

        if ([int]$Group.replicas -eq 1) {
            $BootstrapRequested = $true
        }

        if (Test-ScaleToZeroActive -Group $Group) {
            $Queue = Get-Queue
            if (Test-QueueAttachment -Queue $Queue) {
                Write-Warning (
                    "$Service bootstrap replica and queue attachment verified for protected smoke; " +
                    "the caller must stop the group in a finally block."
                )
                exit 0
            }
            Write-Host (
                "$Service bootstrap replica is active; waiting for queue attachment."
            )
        }
        continue
    }

    if (Test-ScaleToZeroActive -Group $Group) {
        Write-Host (
            "$Service scale-to-zero start accepted; the first queued job may trigger a cold start."
        ) -ForegroundColor Green
        exit 0
    }
}
while ((Get-Date) -lt $Deadline)

if ($AllowBootstrapReplica) {
    throw (
        "Container group did not expose exactly one bootstrap replica with a verified " +
        "queue attachment before timeout."
    )
}
throw "Container group did not activate scale-to-zero state before timeout."
