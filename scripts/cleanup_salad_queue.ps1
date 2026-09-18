[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "flux2_klein", "ltx25")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(10, 600)]
    [int]$TimeoutSeconds = 180,

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
    return ([PSCredential]::new("salad-queue-cleanup", $SecureValue)).GetNetworkCredential().Password
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
    if ($StatusCode -in @(408, 429, 500, 502, 503, 504)) { return $true }
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
        [ValidateRange(1, 120)][int]$TimeoutSec = 30,
        [ValidateRange(1, 10)][int]$MaxAttempts = 6
    )

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt += 1) {
        try {
            return Invoke-RestMethod `
                -Method $Method `
                -Uri $Uri `
                -Headers $Headers `
                -TimeoutSec $TimeoutSec
        }
        catch {
            if (-not (Test-TransientSaladFailure -ErrorRecord $_) -or $Attempt -ge $MaxAttempts) {
                throw
            }
            $DelaySeconds = [Math]::Min(15, 2 * $Attempt)
            Write-Warning (
                "$Service queue cleanup operation '$Operation' failed transiently " +
                "(attempt $Attempt/$MaxAttempts): $($_.Exception.Message). " +
                "Retrying in ${DelaySeconds}s."
            )
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    throw "Unreachable Salad retry state for '$Operation'."
}

function Get-QueueSummary {
    return Invoke-SaladRequest -Uri $QueueUrl -Operation "read queue summary"
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
        Write-Host (
            "Inspecting $Service queue jobs page $Page for active work " +
            "(cleanup deadline in $([int]$SecondsRemaining)s)..."
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
            $Status = [string]$Job.status
            if ($Status -in @("pending", "running")) {
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

function Format-Job {
    param([Parameter(Mandatory)][object]$Job)

    $ApplicationJobId = "-"
    if (
        $Job.PSObject.Properties.Name -contains "input" -and
        $null -ne $Job.input -and
        $Job.input.PSObject.Properties.Name -contains "job_id"
    ) {
        $ApplicationJobId = [string]$Job.input.job_id
    }
    return "transport=$([string]$Job.id) application=$ApplicationJobId status=$([string]$Job.status)"
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
$BaseUrl = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$GroupUrl = "$BaseUrl/containers/$GroupName"
$QueueUrl = "$BaseUrl/queues/$QueueName"
$Headers = @{
    "Salad-Api-Key" = Get-SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-queue-cleanup/1.2"
}

Write-Host "Inspecting $Service container group before queue cleanup..."
$Group = Invoke-SaladRequest -Uri $GroupUrl -Operation "read stopped container group"
$Status = [string]$Group.current_state.status
if ($Status -ne "stopped" -or [bool]$Group.pending_change -or [int]$Group.replicas -ne 0) {
    throw (
        "Queue cleanup requires '$GroupName' fully stopped first; " +
        "status=$Status replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
    )
}

$Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
Write-Host "Inspecting $Service queue summary before historical job pagination..."
$Queue = Get-QueueSummary
if ([int]$Queue.current_queue_length -eq 0) {
    Write-Host "$Service queue cleanup complete: no active or queued jobs." -ForegroundColor Green
    exit 0
}
Write-Warning (
    "$Service queue reports $([int]$Queue.current_queue_length) queued job(s); " +
    "verifying enumerable pending/running jobs within the ${TimeoutSeconds}s cleanup deadline."
)

do {
    $Snapshot = Get-QueueJobSnapshot -Deadline $Deadline
    $ActiveJobs = @($Snapshot.active_jobs)
    if (-not [bool]$Snapshot.complete) {
        if ((Get-Date) -ge $Deadline) {
            break
        }
        Write-Warning (
            "$Service active-job enumeration was incomplete after $([int]$Snapshot.pages) page(s); " +
            "retrying while cleanup time remains."
        )
        Start-Sleep -Seconds 5
        continue
    }

    foreach ($Job in $ActiveJobs) {
        Write-Warning "Cancelling abandoned active job after group stop: $(Format-Job -Job $Job)"
        try {
            Invoke-SaladRequest `
                -Method "Delete" `
                -Uri "$QueueUrl/jobs/$([string]$Job.id)" `
                -Operation "cancel active queue job $([string]$Job.id)" `
                -TimeoutSec 30 |
                Out-Null
        }
        catch {
            if ((Get-HttpStatusCode -ErrorRecord $_) -ne 404) {
                throw
            }
        }
    }

    $Pending = @($ActiveJobs | Where-Object { [string]$_.status -eq "pending" })
    $Running = @($ActiveJobs | Where-Object { [string]$_.status -eq "running" })
    $Queue = Get-QueueSummary

    if ($Pending.Count -eq 0 -and $Running.Count -eq 0) {
        if ([int]$Queue.current_queue_length -ne 0) {
            Write-Warning (
                "$Service queue summary is stale: current_queue_length=" +
                "$([int]$Queue.current_queue_length), but exhaustive job enumeration found " +
                "no pending or running jobs. Treating the stopped queue as logically empty."
            )
        }
        Write-Host "$Service queue cleanup complete: no active or queued jobs." -ForegroundColor Green
        exit 0
    }

    if ((Get-Date) -ge $Deadline) {
        break
    }
    if ($Running.Count -gt 0) {
        Write-Warning (
            "Waiting for cancelled running job(s) to become terminal after group stop: " +
            (($Running | ForEach-Object { Format-Job -Job $_ }) -join "; ")
        )
    }
    elseif ($Pending.Count -gt 0) {
        Write-Warning (
            "Waiting for cancelled pending job(s) to become terminal: " +
            (($Pending | ForEach-Object { Format-Job -Job $_ }) -join "; ")
        )
    }
    Start-Sleep -Seconds 5
}
while ((Get-Date) -lt $Deadline)

$RemainingSnapshot = Get-QueueJobSnapshot -Deadline $Deadline
$Remaining = @($RemainingSnapshot.active_jobs)
$Description = if (-not [bool]$RemainingSnapshot.complete) {
    "active-job enumeration could not complete before the cleanup deadline"
}
elseif ($Remaining.Count -eq 0) {
    "no enumerable active jobs remained, but cleanup could not establish a complete terminal state"
}
else {
    ($Remaining | ForEach-Object { Format-Job -Job $_ }) -join "; "
}
throw "Salad queue cleanup timed out after ${TimeoutSeconds}s: $Description"
