[CmdletBinding()]
param(
    [string]$EnvFile = ".env",

    [ValidateRange(10, 120)]
    [int]$PrewarmTimeoutMinutes = 60,

    [ValidateRange(60, 3600)]
    [int]$RunningTimeoutSeconds = 1200,

    [ValidateRange(30, 900)]
    [int]$PendingTimeoutSeconds = 300,

    [string]$Size = "1024x1024",

    [string]$Prompt = (
        "A cinematic documentary photograph of a windswept lighthouse on a rocky Atlantic " +
        "coast at blue hour, realistic natural textures, restrained color, no text."
    ),

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Prewarm = Join-Path $PSScriptRoot "start_salad_flux_prewarm.ps1"
$Restore = Join-Path $PSScriptRoot "restore_salad_flux_scale_to_zero.ps1"
$Cleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$WorkerManager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
$Smoke = Join-Path $PSScriptRoot "run_flux2_klein_smoke.py"

function Write-Flux2KleinSaladMetrics {
    param([Parameter(Mandatory)][datetime]$StartedAt)

    try {
        if ([string]::IsNullOrWhiteSpace($env:SALAD_API_KEY)) {
            Write-Warning "SALAD_API_KEY is unavailable; skipping Salad log metric collection."
            return
        }

        $Manifest = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\salad\services.json") -Raw |
            ConvertFrom-Json
        $Organization = [string]$Manifest.stack.organization
        $Project = [string]$Manifest.stack.project
        $GroupName = [string]$Manifest.services.flux2_klein.group_name
        $LogsUrl = "https://api.salad.com/api/public/organizations/$Organization/log-entries"
        $Headers = @{
            "Salad-Api-Key" = $env:SALAD_API_KEY
            "Accept" = "application/json"
            "User-Agent" = "ai-video-factory-flux2-klein-smoke/1.0"
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
        $StartTime = $StartedAt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        $Items = @()

        for ($Attempt = 1; $Attempt -le 6; $Attempt += 1) {
            $EndTime = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            $Body = @{
                start_time = $StartTime
                end_time = $EndTime
                page_size = 100
                sort_order = "desc"
                query = $Query
            } | ConvertTo-Json -Depth 5
            $LogRequest = @{
                Method = "Post"
                Uri = $LogsUrl
                Headers = $Headers
                ContentType = "application/json"
                Body = $Body
                TimeoutSec = 30
            }
            $Response = Invoke-RestMethod @LogRequest
            $Items = @($Response.items)
            $HasRuntime = @($Items | Where-Object {
                [string]$_.text_log -like "*FLUX2_KLEIN_RUNTIME_READY*"
            }).Count -gt 0
            $HasInference = @($Items | Where-Object {
                [string]$_.text_log -like "*FLUX2_KLEIN_INFERENCE_METRIC*"
            }).Count -gt 0
            if ($HasRuntime -and $HasInference) {
                break
            }
            if ($Attempt -lt 6) {
                Start-Sleep -Seconds 5
            }
        }

        foreach ($Item in $Items) {
            if (-not [string]::IsNullOrWhiteSpace([string]$Item.text_log)) {
                Write-Host ("SALAD_LOG_METRIC time={0} {1}" -f $Item.time, $Item.text_log)
            }
        }
        if ($Items.Count -eq 0) {
            Write-Warning "No FLUX.2 Klein container metrics were returned by the Salad log API."
        }
    }
    catch {
        Write-Warning "Salad log metric collection failed: $($_.Exception.Message)"
    }
}

$PrewarmArguments = @{
    EnvFile = $EnvFile
    TimeoutMinutes = $PrewarmTimeoutMinutes
}
$RestoreArguments = @{
    EnvFile = $EnvFile
    TimeoutSeconds = 180
}
if ($NonInteractive) {
    $PrewarmArguments["NonInteractive"] = $true
    $RestoreArguments["NonInteractive"] = $true
}

$PrimaryFailure = $null
$CleanupFailures = @()
$Touched = $false
$BenchmarkStartedAt = (Get-Date).ToUniversalTime().AddMinutes(-1)
Set-Location $RepoRoot

try {
    $Touched = $true
    Write-Host "=== FLUX.2 Klein prewarm from zero replicas ===" -ForegroundColor Cyan
    & $Prewarm @PrewarmArguments
    if (-not $?) {
        throw "FLUX.2 Klein prewarm failed."
    }

    Write-Host "=== FLUX.2 Klein text-to-image Salad/R2 smoke ===" -ForegroundColor Cyan
    & python $Smoke "--prompt" $Prompt "--size" $Size "--pending-timeout-seconds" $PendingTimeoutSeconds "--timeout-seconds" $RunningTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "FLUX.2 Klein smoke failed with exit code $LASTEXITCODE."
    }
}
catch {
    $PrimaryFailure = $_
}
finally {
    if ($Touched) {
        Write-Flux2KleinSaladMetrics -StartedAt $BenchmarkStartedAt
        try {
            & $Restore @RestoreArguments
        }
        catch {
            Write-Warning "FLUX.2 Klein scale-to-zero restore failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
        try {
            $CleanupArguments = @{
                Service = "flux2_klein"
                TimeoutSeconds = 180
            }
            if ($NonInteractive) {
                $CleanupArguments["NonInteractive"] = $true
            }
            & $Cleanup @CleanupArguments
        }
        catch {
            Write-Warning "FLUX.2 Klein queue cleanup failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
        try {
            $StatusArguments = @{
                Action = "Status"
                Service = "flux2_klein"
                EnvFile = $EnvFile
            }
            if ($NonInteractive) {
                $StatusArguments["NonInteractive"] = $true
            }
            & $WorkerManager @StatusArguments
        }
        catch {
            Write-Warning "FLUX.2 Klein final status verification failed: $($_.Exception.Message)"
            $CleanupFailures += $_
        }
    }
}

if ($null -ne $PrimaryFailure) {
    if ($CleanupFailures.Count -gt 0) {
        Write-Warning (
            "Cleanup also reported $($CleanupFailures.Count) failure(s); " +
            "preserving the original FLUX.2 Klein smoke failure."
        )
    }
    throw $PrimaryFailure
}
if ($CleanupFailures.Count -gt 0) {
    throw $CleanupFailures[0]
}

Write-Host (
    "FLUX.2 Klein smoke complete; fallback group restored to manifest scale-to-zero."
) -ForegroundColor Green
