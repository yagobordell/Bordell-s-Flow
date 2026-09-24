[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "qwen_image_21", "ltx25", "realesrgan")]
    [string]$Service,

    [string]$EnvFile = ".env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
. (Join-Path $RepoRoot "scripts/salad/_env_file.ps1")
Import-EnvFile -Path $EnvFile

if ([string]::IsNullOrWhiteSpace($env:SALAD_API_KEY)) {
    throw "SALAD_API_KEY must be available in the process or .env."
}

$Manifest = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy/salad/services.json") -Raw |
    ConvertFrom-Json
$Definition = $Manifest.services.$Service
$BaseUrl = (
    "https://api.salad.com/api/public/organizations/{0}/projects/{1}" -f
    [string]$Manifest.stack.organization,
    [string]$Manifest.stack.project
)
$GroupUrl = "$BaseUrl/containers/$([string]$Definition.group_name)"
$QueueUrl = "$BaseUrl/queues/$([string]$Definition.queue_name)"
$Headers = @{
    "Salad-Api-Key" = $env:SALAD_API_KEY
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-queue-inspector/1.0"
}

function Read-Group {
    return Invoke-RestMethod -Method Get -Uri $GroupUrl -Headers $Headers -TimeoutSec 30
}

function Read-Queue {
    return Invoke-RestMethod -Method Get -Uri $QueueUrl -Headers $Headers -TimeoutSec 30
}

function Read-JobInventory {
    $Rows = [Collections.Generic.List[object]]::new()
    $PageSize = 100
    for ($Page = 1; $Page -le 100; $Page++) {
        $Response = Invoke-RestMethod -Method Get -Headers $Headers -TimeoutSec 30 `
            -Uri "$QueueUrl/jobs?page=$Page&page_size=$PageSize"
        if ($Response.PSObject.Properties.Name -notcontains "items") {
            throw "Salad job listing omitted its documented items field; inventory is incomplete."
        }
        $Items = @($Response.items | Where-Object { $null -ne $_ })
        foreach ($Job in $Items) {
            $Rows.Add([PSCustomObject]@{
                id = [string]$Job.id
                status = [string]$Job.status
                create_time = [string]$Job.create_time
            })
        }
        if ($Items.Count -lt $PageSize) {
            return [PSCustomObject]@{
                jobs = @($Rows.ToArray())
                pages = $Page
                complete = $true
            }
        }
    }
    return [PSCustomObject]@{
        jobs = @($Rows.ToArray())
        pages = 100
        complete = $false
    }
}

# Read only: never download inputs or outputs, patch groups, or cancel jobs.
$GroupBefore = Read-Group
$QueueBefore = Read-Queue
$Inventory = Read-JobInventory
$QueueAfter = Read-Queue
$GroupAfter = Read-Group
$Jobs = @($Inventory.jobs)
$Active = @($Jobs | Where-Object { $_.status -in @("pending", "running") })
$Other = @(
    $Jobs | Where-Object {
        $_.status -notin @("pending", "running", "succeeded", "cancelled", "failed")
    }
)
$Autoscaler = $GroupAfter.PSObject.Properties["queue_autoscaler"]
$Minimum = if ($null -ne $Autoscaler -and $null -ne $Autoscaler.Value) {
    [string]$Autoscaler.Value.min_replicas
}
else {
    "not exposed"
}

Write-Host "=== SALAD QUEUE INVENTORY: $Service ==="
Write-Host "group.status=$([string]$GroupAfter.current_state.status)"
Write-Host "group.replicas.before=$([int]$GroupBefore.replicas)"
Write-Host "group.replicas.after=$([int]$GroupAfter.replicas)"
Write-Host "group.pending_change=$([bool]$GroupAfter.pending_change)"
Write-Host "group.autoscaler.min_replicas=$Minimum"
Write-Host "queue.length.before=$([int]$QueueBefore.current_queue_length)"
Write-Host "queue.length.after=$([int]$QueueAfter.current_queue_length)"
Write-Host "queue.pages_scanned=$([int]$Inventory.pages)"
Write-Host "queue.pagination_complete=$([bool]$Inventory.complete)"
Write-Host "queue.jobs_listed=$($Jobs.Count)"
Write-Host "queue.active_jobs_listed=$($Active.Count)"

foreach ($Status in @("pending", "running", "succeeded", "cancelled", "failed")) {
    $Count = @($Jobs | Where-Object { $_.status -eq $Status }).Count
    Write-Host "queue.jobs.$Status=$Count"
}
if ($Other.Count -gt 0) {
    Write-Warning (
        "Salad returned undocumented statuses: " +
        (($Other | Group-Object status | ForEach-Object { $_.Name }) -join ", ")
    )
}
if ($Active.Count -gt 0) {
    Write-Host "Active transport job IDs, statuses and creation times (no job input/output):"
    $Active | Select-Object id, status, create_time | Format-Table -AutoSize
}
if (
    [bool]$Inventory.complete -and
    $Active.Count -eq 0 -and
    [int]$QueueAfter.current_queue_length -gt 0
) {
    Write-Warning (
        "DISCREPANCY: the complete job listing contains no pending/running work, " +
        "but the queue reports a nonzero current_queue_length. Do not delete jobs " +
        "or override the benchmark replica guard on this evidence alone."
    )
}
if (-not [bool]$Inventory.complete) {
    throw "Salad job listing exceeded the bounded pagination limit; no empty-queue conclusion."
}
Write-Host "Read-only diagnostic complete; Salad resources were not modified."
