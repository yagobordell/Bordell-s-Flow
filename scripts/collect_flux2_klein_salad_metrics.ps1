[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [ValidateRange(1, 1440)]
    [int]$SinceMinutes = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$ReportPath = Join-Path $RepoRoot "data\output\flux2-klein-smoke\report.json"

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

Import-EnvFile -Path $EnvFile

if ([string]::IsNullOrWhiteSpace($env:SALAD_API_KEY)) {
    throw "SALAD_API_KEY is missing."
}
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad manifest not found: $ManifestPath"
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$Organization = [string]$Manifest.stack.organization
$Project = [string]$Manifest.stack.project
$GroupName = [string]$Manifest.services.flux2_klein.group_name
$LogsUrl = "https://api.salad.com/api/public/organizations/$Organization/log-entries"
$Headers = @{
    "Salad-Api-Key" = $env:SALAD_API_KEY
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-flux2-klein-metrics/1.0"
}
$Query = (
    'resource.type = "container" and ' +
    'resource.labels.project_name = "' + $Project + '" and ' +
    'resource.labels.container_group_name = "' + $GroupName + '" and ' +
    '(' +
    'log contains "FLUX2_KLEIN_RUNTIME_READY" or ' +
    'log contains "FLUX2_KLEIN_INFERENCE_METRIC" or ' +
    'log contains "Diffusers snapshot complete" or ' +
    'log contains "model bootstrap complete"' +
    ')'
)

$End = (Get-Date).ToUniversalTime()
$Start = $End.AddMinutes(-$SinceMinutes)
$Body = @{
    start_time = $Start.ToString("yyyy-MM-ddTHH:mm:ssZ")
    end_time = $End.ToString("yyyy-MM-ddTHH:mm:ssZ")
    page_size = 100
    sort_order = "desc"
    query = $Query
} | ConvertTo-Json -Depth 5

$Response = Invoke-RestMethod @{
    Method = "Post"
    Uri = $LogsUrl
    Headers = $Headers
    ContentType = "application/json"
    Body = $Body
    TimeoutSec = 30
}
$Items = @($Response.items) | Sort-Object time

foreach ($Item in $Items) {
    if (-not [string]::IsNullOrWhiteSpace([string]$Item.text_log)) {
        Write-Host ("SALAD_LOG_METRIC time={0} {1}" -f $Item.time, $Item.text_log)
    }
}

$RuntimeItems = @($Items | Where-Object {
    [string]$_.text_log -like "*FLUX2_KLEIN_RUNTIME_READY*"
})
$InferenceItems = @($Items | Where-Object {
    [string]$_.text_log -like "*FLUX2_KLEIN_INFERENCE_METRIC*"
})

if ($RuntimeItems.Count -eq 0) {
    throw "No FLUX2_KLEIN_RUNTIME_READY metric found in the requested Salad log window."
}
if ($InferenceItems.Count -eq 0) {
    throw "No FLUX2_KLEIN_INFERENCE_METRIC metric found in the requested Salad log window."
}

Write-Host (
    "FLUX2_KLEIN_HISTORICAL_METRICS runtime_count={0} inference_count={1} since_minutes={2}" -f
    $RuntimeItems.Count,
    $InferenceItems.Count,
    $SinceMinutes
) -ForegroundColor Green

if (Test-Path -LiteralPath $ReportPath -PathType Leaf) {
    $Report = Get-Content -LiteralPath $ReportPath -Raw
    Write-Host "FLUX2_KLEIN_SMOKE_REPORT $Report"
}
else {
    Write-Warning "Smoke report not found locally: $ReportPath"
}
