[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [ValidateRange(1, 1440)]
    [int]$SinceMinutes = 60,

    [string]$StartTimeUtc = "",

    [string]$EndTimeUtc = "",

    [ValidateRange(1, 15)]
    [int]$WindowMinutes = 2,

    [ValidateRange(1, 5)]
    [int]$RetryCount = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$ReportPath = Join-Path $RepoRoot "data\output\fish-speech-smoke\report.json"

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

function Invoke-SaladMarkerQuery {
    param(
        [Parameter(Mandatory)][string]$Marker,
        [Parameter(Mandatory)][datetime]$RangeStart,
        [Parameter(Mandatory)][datetime]$RangeEnd
    )

    $Query = (
        'resource.type = "container" and ' +
        'resource.labels.project_name = "' + $Project + '" and ' +
        'resource.labels.container_group_name = "' + $GroupName + '" and ' +
        'log contains "' + $Marker + '"'
    )
    $Collected = @()
    $WindowStart = $RangeStart

    while ($WindowStart -lt $RangeEnd) {
        $WindowEnd = $WindowStart.AddMinutes($WindowMinutes)
        if ($WindowEnd -gt $RangeEnd) {
            $WindowEnd = $RangeEnd
        }

        $Succeeded = $false
        for ($Attempt = 1; $Attempt -le $RetryCount; $Attempt += 1) {
            $Body = @{
                start_time = $WindowStart.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                end_time = $WindowEnd.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                page_size = 25
                sort_order = "desc"
                query = $Query
            } | ConvertTo-Json -Depth 5
            $LogRequest = @{
                Method = "Post"
                Uri = $LogsUrl
                Headers = $Headers
                ContentType = "application/json"
                Body = $Body
                TimeoutSec = 20
            }

            try {
                $Response = Invoke-RestMethod @LogRequest
                $Collected += @($Response.items)
                $Succeeded = $true
                break
            }
            catch {
                if ($Attempt -ge $RetryCount) {
                    throw
                }
                Write-Warning (
                    "Salad log query retry marker='$Marker' window=$($WindowStart.ToString('o')).." +
                    "$($WindowEnd.ToString('o')) attempt=$Attempt/${RetryCount}: $($_.Exception.Message)"
                )
                Start-Sleep -Seconds ([Math]::Min(10, 2 * $Attempt))
            }
        }

        if (-not $Succeeded) {
            throw "Salad log query did not complete for marker '$Marker'."
        }
        $WindowStart = $WindowEnd
    }

    return @($Collected)
}

Import-EnvFile -Path $EnvFile

if ([string]::IsNullOrWhiteSpace($env:SALAD_API_KEY)) {
    throw "SALAD_API_KEY is missing."
}
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad manifest not found: $ManifestPath"
}
if (
    [string]::IsNullOrWhiteSpace($StartTimeUtc) -xor
    [string]::IsNullOrWhiteSpace($EndTimeUtc)
) {
    throw "Provide both -StartTimeUtc and -EndTimeUtc, or neither."
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$Organization = [string]$Manifest.stack.organization
$Project = [string]$Manifest.stack.project
$GroupName = [string]$Manifest.services.fish_speech.group_name
$LogsUrl = "https://api.salad.com/api/public/organizations/$Organization/log-entries"
$Headers = @{
    "Salad-Api-Key" = $env:SALAD_API_KEY
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-fish-speech-metrics/1.1"
}

if (-not [string]::IsNullOrWhiteSpace($StartTimeUtc)) {
    $Start = [DateTimeOffset]::Parse($StartTimeUtc).UtcDateTime
    $End = [DateTimeOffset]::Parse($EndTimeUtc).UtcDateTime
}
else {
    $End = (Get-Date).ToUniversalTime()
    $Start = $End.AddMinutes(-$SinceMinutes)
}
if ($End -le $Start) {
    throw "End time must be later than start time."
}

$Markers = @(
    "FISH_SPEECH_RUNTIME_READY",
    "FISH_SPEECH_INFERENCE_METRIC",
    "FISH_SPEECH_CHECKPOINT_READY",
    "Fish Speech model bootstrap complete"
)
$AllItems = @()
foreach ($Marker in $Markers) {
    Write-Host (
        "Collecting Salad marker '$Marker' in $WindowMinutes-minute windows " +
        "from $($Start.ToString('o')) to $($End.ToString('o'))..."
    )
    $AllItems += @(Invoke-SaladMarkerQuery -Marker $Marker -RangeStart $Start -RangeEnd $End)
}

$Items = @($AllItems | Sort-Object -Property time, text_log -Unique)

foreach ($Item in $Items) {
    if (-not [string]::IsNullOrWhiteSpace([string]$Item.text_log)) {
        Write-Host ("SALAD_LOG_METRIC time={0} {1}" -f $Item.time, $Item.text_log)
    }
}

$RuntimeItems = @($Items | Where-Object {
    [string]$_.text_log -like "*FISH_SPEECH_RUNTIME_READY*"
})
$InferenceItems = @($Items | Where-Object {
    [string]$_.text_log -like "*FISH_SPEECH_INFERENCE_METRIC*"
})

if ($RuntimeItems.Count -eq 0) {
    throw "No FISH_SPEECH_RUNTIME_READY metric found in the requested Salad log window."
}
if ($InferenceItems.Count -eq 0) {
    throw "No FISH_SPEECH_INFERENCE_METRIC metric found in the requested Salad log window."
}

$HistoricalMetric = (
    "FISH_SPEECH_HISTORICAL_METRICS runtime_count={0} inference_count={1} " +
    "start={2} end={3}"
) -f @(
    $RuntimeItems.Count,
    $InferenceItems.Count,
    $Start.ToString("o"),
    $End.ToString("o")
)
Write-Host $HistoricalMetric -ForegroundColor Green

if (Test-Path -LiteralPath $ReportPath -PathType Leaf) {
    $Report = Get-Content -LiteralPath $ReportPath -Raw
    Write-Host "FISH_SPEECH_SMOKE_REPORT $Report"
}
else {
    Write-Warning "Smoke report not found locally: $ReportPath"
}
