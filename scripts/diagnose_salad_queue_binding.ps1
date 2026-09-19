[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "flux2_klein", "ltx25", "realesrgan")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(15, 1440)]
    [int]$LookbackMinutes = 180,

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

function Get-SaladApiKey {
    $Value = [Environment]::GetEnvironmentVariable("SALAD_API_KEY")
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value.Trim()
    }
    if ($NonInteractive) {
        throw "SALAD_API_KEY is missing and -NonInteractive was requested."
    }
    $SecureValue = Read-Host "Salad API key" -AsSecureString
    $Credential = [PSCredential]::new("salad-queue-diagnostic", $SecureValue)
    $Value = $Credential.GetNetworkCredential().Password
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "SALAD_API_KEY is empty."
    }
    return $Value.Trim()
}

function Get-OptionalProperty {
    param(
        [Parameter(Mandatory)][object]$Object,
        [Parameter(Mandatory)][string]$Name
    )
    $Property = $Object.PSObject.Properties[$Name]
    if ($null -eq $Property) {
        return $null
    }
    return $Property.Value
}

function Get-LogText {
    param([Parameter(Mandatory)][object]$Entry)

    $Text = Get-OptionalProperty -Object $Entry -Name "text_log"
    if (-not [string]::IsNullOrWhiteSpace([string]$Text)) {
        return [string]$Text
    }
    $Json = Get-OptionalProperty -Object $Entry -Name "json_log"
    if ($null -ne $Json) {
        return ($Json | ConvertTo-Json -Depth 8 -Compress)
    }
    return ""
}

Import-EnvFile -Path $EnvFile
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad stack manifest not found: $ManifestPath"
}

$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$DefinitionProperty = $Document.services.PSObject.Properties[$Service]
if ($null -eq $DefinitionProperty) {
    throw "Unknown Salad service '$Service'."
}
$Definition = $DefinitionProperty.Value
$Organization = [string]$Document.stack.organization
$Project = [string]$Document.stack.project
$GroupName = [string]$Definition.group_name
$QueueName = [string]$Definition.queue_name
$Base = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$GroupUrl = "$Base/containers/$GroupName"
$QueueUrl = "$Base/queues/$QueueName"
$LogsUrl = "https://api.salad.com/api/public/organizations/$Organization/log-entries"
$Headers = @{
    "Salad-Api-Key" = Get-SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-queue-diagnostic/1.0"
}

Write-Host "=== Salad Job Queue binding diagnostic : $Service ===" -ForegroundColor Cyan
$Group = Invoke-RestMethod -Method Get -Uri $GroupUrl -Headers $Headers -TimeoutSec 30
$Queue = Invoke-RestMethod -Method Get -Uri $QueueUrl -Headers $Headers -TimeoutSec 30

$Connection = Get-OptionalProperty -Object $Group -Name "queue_connection"
$Autoscaler = Get-OptionalProperty -Object $Group -Name "queue_autoscaler"
$Autostart = Get-OptionalProperty -Object $Group -Name "autostart_policy"
$Status = ""
$CurrentState = Get-OptionalProperty -Object $Group -Name "current_state"
if ($null -ne $CurrentState) {
    $Status = [string](Get-OptionalProperty -Object $CurrentState -Name "status")
}

$ConnectionQueue = ""
$ConnectionPath = ""
$ConnectionPort = 0
if ($null -ne $Connection) {
    $ConnectionQueue = [string](Get-OptionalProperty -Object $Connection -Name "queue_name")
    $ConnectionPath = [string](Get-OptionalProperty -Object $Connection -Name "path")
    $PortValue = Get-OptionalProperty -Object $Connection -Name "port"
    if ($null -ne $PortValue) {
        $ConnectionPort = [int]$PortValue
    }
}

$AssociatedGroups = @()
$QueueGroups = Get-OptionalProperty -Object $Queue -Name "container_groups"
if ($null -ne $QueueGroups) {
    $AssociatedGroups = @(
        @($QueueGroups) |
            ForEach-Object { [string](Get-OptionalProperty -Object $_ -Name "name") } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
}
$Attached = $AssociatedGroups -contains $GroupName

Write-Host ("group.name={0}" -f $GroupName)
Write-Host ("group.version={0}" -f [int]$Group.version)
Write-Host ("group.status={0}" -f $Status)
Write-Host ("group.replicas={0}" -f [int]$Group.replicas)
Write-Host ("group.pending_change={0}" -f [bool]$Group.pending_change)
Write-Host ("group.autostart_policy={0}" -f $Autostart)
Write-Host ("group.queue_connection.queue_name={0}" -f $ConnectionQueue)
Write-Host ("group.queue_connection.path={0}" -f $ConnectionPath)
Write-Host ("group.queue_connection.port={0}" -f $ConnectionPort)
if ($null -eq $Autoscaler) {
    Write-Host "group.queue_autoscaler=<missing>" -ForegroundColor Yellow
}
else {
    Write-Host (
        "group.queue_autoscaler=min:{0} max:{1} desired:{2} poll:{3}" -f
        [int]$Autoscaler.min_replicas,
        [int]$Autoscaler.max_replicas,
        [int]$Autoscaler.desired_queue_length,
        [int]$Autoscaler.polling_period
    )
}
Write-Host ("queue.name={0}" -f [string]$Queue.name)
Write-Host ("queue.id={0}" -f [string]$Queue.id)
Write-Host ("queue.current_queue_length={0}" -f [int]$Queue.current_queue_length)
Write-Host ("queue.container_groups.count={0}" -f $AssociatedGroups.Count)
Write-Host ("queue.container_groups={0}" -f ($AssociatedGroups -join ","))
Write-Host ("binding.attached={0}" -f $Attached)

Write-Host "=== Recent queue jobs ===" -ForegroundColor Cyan
$SucceededJobCount = 0
try {
    $Jobs = Invoke-RestMethod `
        -Method Get `
        -Uri "$QueueUrl/jobs?page=1&page_size=10" `
        -Headers $Headers `
        -TimeoutSec 30
    $RecentJobs = @(@($Jobs.items) | Select-Object -First 10)
    $SucceededJobCount = @(
        $RecentJobs | Where-Object { [string]$_.status -eq "succeeded" }
    ).Count
    if ($RecentJobs.Count -eq 0) {
        Write-Host "queue.jobs=<none>"
    }
    else {
        $RecentJobs |
            Select-Object id, status, create_time, update_time |
            Format-Table -AutoSize
    }
}
catch {
    Write-Warning ("Could not list recent queue jobs: " + $_.Exception.Message)
}

if ($null -eq $Connection) {
    Write-Host "DIAGNOSIS=group_queue_connection_missing" -ForegroundColor Red
}
elseif ($ConnectionQueue -ne $QueueName) {
    Write-Host "DIAGNOSIS=group_queue_connection_mismatch" -ForegroundColor Red
}
elseif ($Attached) {
    Write-Host "DIAGNOSIS=association_visible" -ForegroundColor Green
}
elseif ($SucceededJobCount -gt 0) {
    Write-Host "DIAGNOSIS=queue_listing_non_authoritative_routing_proven" -ForegroundColor Green
}
else {
    Write-Host "DIAGNOSIS=queue_listing_absent_runtime_unproven" -ForegroundColor Yellow
}

Write-Host "=== Recent container logs relevant to queue transport ===" -ForegroundColor Cyan
$End = [DateTime]::UtcNow
$Start = $End.AddMinutes(-$LookbackMinutes)
$Query = (
    'resource.type = "container" and ' +
    'resource.labels.project_name = "' + $Project + '" and ' +
    'resource.labels.container_group_name = "' + $GroupName + '"'
)
$LogBody = @{
    start_time = $Start.ToString("yyyy-MM-ddTHH:mm:ssZ")
    end_time = $End.ToString("yyyy-MM-ddTHH:mm:ssZ")
    page_size = 100
    sort_order = "desc"
    query = $Query
} | ConvertTo-Json -Depth 5

try {
    $LogHeaders = @{
        "Salad-Api-Key" = $Headers["Salad-Api-Key"]
        "Accept" = "application/json"
        "Content-Type" = "application/json"
        "User-Agent" = "ai-video-factory-queue-diagnostic/1.0"
    }
    $Logs = Invoke-RestMethod `
        -Method Post `
        -Uri $LogsUrl `
        -Headers $LogHeaders `
        -Body $LogBody `
        -TimeoutSec 60

    $InterestingPattern = "(?i)queue|salad|heartbeat|worker|connect|ready|error|grpc|transport"
    $Relevant = @()
    foreach ($Entry in @($Logs.items)) {
        $Text = Get-LogText -Entry $Entry
        if ($Text -match $InterestingPattern) {
            $Relevant += [PSCustomObject]@{
                time = [string]$Entry.time
                severity = [string]$Entry.severity
                message = $Text
            }
        }
    }

    if ($Relevant.Count -eq 0) {
        Write-Host "No queue-relevant container logs found in the selected lookback window." -ForegroundColor Yellow
        Write-Host ("container_logs_returned={0}" -f @($Logs.items).Count)
    }
    else {
        $Relevant | Select-Object -First 40 | Format-Table -Wrap -AutoSize
    }
}
catch {
    Write-Warning ("Could not query Salad container logs: " + $_.Exception.Message)
}

Write-Host "Diagnostic complete; no Salad resources were mutated." -ForegroundColor Green
