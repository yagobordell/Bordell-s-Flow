[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "ideogram4", "ltx25")]
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

function Get-ActiveQueueJobs {
    $Active = @()
    for ($Page = 1; $Page -le 100; $Page += 1) {
        $Response = Invoke-RestMethod `
            -Uri "$QueueUrl/jobs?page=$Page&page_size=25" `
            -Headers $Headers `
            -TimeoutSec 30
        $Items = if ($Response.PSObject.Properties.Name -contains "items") {
            @($Response.items)
        }
        elseif ($Response.PSObject.Properties.Name -contains "jobs") {
            @($Response.jobs)
        }
        else {
            @()
        }
        foreach ($Job in $Items) {
            $Status = [string]$Job.status
            if ($Status -in @("pending", "running")) {
                $Active += $Job
            }
        }
        if ($Items.Count -lt 25) {
            break
        }
    }
    return @($Active)
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
    "User-Agent" = "ai-video-factory-queue-cleanup/1.0"
}

$Group = Invoke-RestMethod -Uri $GroupUrl -Headers $Headers -TimeoutSec 30
$Status = [string]$Group.current_state.status
if ($Status -ne "stopped" -or [bool]$Group.pending_change -or [int]$Group.replicas -ne 0) {
    throw (
        "Queue cleanup requires '$GroupName' fully stopped first; " +
        "status=$Status replicas=$([int]$Group.replicas) pending=$([bool]$Group.pending_change)."
    )
}

$Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    $ActiveJobs = @(Get-ActiveQueueJobs)
    foreach ($Job in @($ActiveJobs | Where-Object { [string]$_.status -eq "pending" })) {
        Write-Warning "Cancelling abandoned pending job: $(Format-Job -Job $Job)"
        Invoke-RestMethod `
            -Method Delete `
            -Uri "$QueueUrl/jobs/$([string]$Job.id)" `
            -Headers $Headers `
            -TimeoutSec 30 |
            Out-Null
    }

    $Running = @($ActiveJobs | Where-Object { [string]$_.status -eq "running" })
    if ($Running.Count -eq 0) {
        $Queue = Invoke-RestMethod -Uri $QueueUrl -Headers $Headers -TimeoutSec 30
        if ([int]$Queue.current_queue_length -eq 0) {
            Write-Host "$Service queue cleanup complete: no active or queued jobs." -ForegroundColor Green
            exit 0
        }
    }

    if ((Get-Date) -ge $Deadline) {
        break
    }
    if ($Running.Count -gt 0) {
        Write-Warning (
            "Waiting for dispatched job(s) to become terminal after group stop: " +
            (($Running | ForEach-Object { Format-Job -Job $_ }) -join "; ")
        )
    }
    Start-Sleep -Seconds 5
}
while ((Get-Date) -lt $Deadline)

$Remaining = @(Get-ActiveQueueJobs)
$Description = if ($Remaining.Count -eq 0) {
    "queue length remained non-zero with no enumerable active jobs"
}
else {
    ($Remaining | ForEach-Object { Format-Job -Job $_ }) -join "; "
}
throw "Salad queue cleanup timed out after ${TimeoutSeconds}s: $Description"
