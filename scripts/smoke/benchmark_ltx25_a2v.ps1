[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$AvatarImage,
    [Parameter(Mandatory)][string]$Audio,
    [string]$Prompt = "An elderly monk speaking calmly to the camera.",
    [ValidateRange(0, 2147483647)][int]$Seed = 4242,
    [string]$EnvFile = ".env",
    [string]$OutputDir = "data/output/deployment-validation/ltx25-a2v-benchmark"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
$Manager = Join-Path $RepoRoot "scripts/salad/manage_salad_worker.ps1"
$Bootstrap = Join-Path $RepoRoot "scripts/salad/start_salad_protected_smoke.ps1"
$Submit = Join-Path $RepoRoot "scripts/smoke/submit_ltx25_a2v_smoke.py"

function Resolve-LocalPath {
    param([Parameter(Mandatory)][string]$Path)
    if ([IO.Path]::IsPathRooted($Path)) {
        return [IO.Path]::GetFullPath($Path)
    }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $Path))
}

$AvatarImage = Resolve-LocalPath $AvatarImage
$Audio = Resolve-LocalPath $Audio
$EnvFile = Resolve-LocalPath $EnvFile
$OutputDir = Resolve-LocalPath $OutputDir

foreach ($Path in @($AvatarImage, $Audio, $Manager, $Bootstrap, $Submit)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required benchmark file not found: $Path"
    }
}
foreach ($Command in @("docker", "uv", "ffprobe")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "Required benchmark executable not found: $Command"
    }
}

. (Join-Path $RepoRoot "scripts/salad/_env_file.ps1")
Import-EnvFile -Path $EnvFile
$Manifest = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy/salad/services.json") -Raw |
    ConvertFrom-Json
$Definition = $Manifest.services.ltx25
$RequiredEnv = @(
    "SALAD_API_KEY", "SALAD_ORGANIZATION", "SALAD_PROJECT",
    "R2_ENDPOINT_URL", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"
) + @($Manifest.stack.shared_required_environment) + @($Definition.required_environment)
$Missing = @($RequiredEnv | Select-Object -Unique | Where-Object {
    [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable([string]$_))
})
if ($Missing.Count -gt 0) {
    throw "Missing benchmark environment variables: $($Missing -join ', ')"
}
if (
    $env:SALAD_ORGANIZATION -ne [string]$Manifest.stack.organization -or
    $env:SALAD_PROJECT -ne [string]$Manifest.stack.project
) {
    throw "Salad organization/project in the environment differ from deploy/salad/services.json."
}

$Probe = & ffprobe -v error -show_entries format=duration -of "default=noprint_wrappers=1:nokey=1" $Audio
if ($LASTEXITCODE -ne 0 -or -not $Probe) {
    throw "Could not measure the benchmark audio with ffprobe."
}
$AudioSeconds = [double]::Parse(
    [string](@($Probe)[0]),
    [Globalization.CultureInfo]::InvariantCulture
)
if ($AudioSeconds -lt 4.0 -or $AudioSeconds -gt 6.0) {
    throw "Benchmark audio must be approximately five seconds (4-6 s); got $AudioSeconds s."
}

# Refuse to take ownership of a worker already running somebody else's jobs.
$GroupUrl = "https://api.salad.com/api/public/organizations/$($Manifest.stack.organization)/" +
    "projects/$($Manifest.stack.project)/containers/$($Definition.group_name)"
$Headers = @{
    "Salad-Api-Key" = $env:SALAD_API_KEY
    "Accept" = "application/json"
}
$Group = $null
try {
    $Group = Invoke-RestMethod -Method Get -Uri $GroupUrl -Headers $Headers -TimeoutSec 30
}
catch {
    $StatusCode = $_.Exception.Response.StatusCode
    if ($null -eq $StatusCode -or [int]$StatusCode -ne 404) {
        throw
    }
}
if (
    $null -ne $Group -and (
        [string]$Group.current_state.status -ne "stopped" -or
        [int]$Group.replicas -ne 0 -or
        [bool]$Group.pending_change
    )
) {
    throw "LTX worker is not stopped with zero replicas; refusing to interrupt existing work."
}

$BatchId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ") + "-" +
    [guid]::NewGuid().ToString("N").Substring(0, 8)
$BatchDir = Join-Path $OutputDir $BatchId
New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null
$Runs = [Collections.Generic.List[object]]::new()
$Failure = $null
$StopError = $null
$Stopped = $false
$OwnsWorker = $false
$Common = @{ Service = "ltx25"; EnvFile = $EnvFile; NonInteractive = $true }

try {
    $OwnsWorker = $true
    Write-Host "Building and publishing manifest image: $($Definition.image)"
    & $Manager @Common -Action Prepare
    if (-not $?) { throw "LTX Prepare failed." }

    Write-Host "Starting one protected RTX 5090 worker for all five runs."
    & $Bootstrap @Common -TimeoutMinutes 60 -RunningNotReadyTimeoutMinutes 60
    if (-not $?) { throw "LTX protected bootstrap failed." }

    for ($Index = 1; $Index -le 5; $Index++) {
        $SegmentId = "a2v-benchmark-$BatchId-$Index"
        $RunDir = Join-Path $BatchDir ("run-{0:00}" -f $Index)
        New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
        Write-Host "Generation $Index/5; segment_id=$SegmentId"

        $SubmitArgs = @(
            $Submit, "--avatar-image", $AvatarImage, "--audio", $Audio,
            "--prompt", $Prompt, "--segment-id", $SegmentId, "--profile", "fast",
            "--seed", "$Seed", "--width", "1280", "--height", "720", "--fps", "24",
            "--timeout-seconds", "600", "--max-pending-reallocations", "0",
            "--output-dir", $RunDir
        )
        & uv run --no-sync python @SubmitArgs
        if (-not $?) { throw "A2V submission/validation failed for generation $Index." }

        $Metadata = Get-Content -LiteralPath (Join-Path $RunDir "metadata.json") -Raw |
            ConvertFrom-Json
        $Row = [pscustomobject]@{
            generation = $Index
            warmup = ($Index -eq 1)
            segment_id = $SegmentId
            inference_seconds = [double]$Metadata.inference_seconds
            encode_mux_seconds = [double]$Metadata.video_encode_mux_seconds
            total_seconds = [double]$Metadata.total_elapsed_seconds
            peak_vram_bytes = $Metadata.peak_vram_bytes
            pipeline_reused = [bool]$Metadata.pipeline_reused
            stage_1_steps = [int]$Metadata.stage_1_steps
            stage_2_steps = [int]$Metadata.stage_2_steps
            transformer_variant = [string]$Metadata.transformer_variant
            video = (Join-Path $RunDir "avatar_segment.mp4")
        }
        $Runs.Add($Row)
        if (
            $Row.stage_1_steps -ne 8 -or $Row.stage_2_steps -ne 3 -or
            $Row.transformer_variant -ne "distilled"
        ) {
            throw "Generation $Index did not use the expected distilled 8+3-step profile."
        }
        if ($Index -gt 1 -and -not $Row.pipeline_reused) {
            throw "Generation $Index rebuilt its pipeline; warm benchmark is not comparable."
        }
    }
}
catch {
    $Failure = $_
}
finally {
    if ($OwnsWorker) {
        try {
            Write-Host "Stopping the worker and restoring zero replicas."
            & $Manager @Common -Action Stop
            if (-not $?) { throw "LTX worker stop failed." }
            $Stopped = $true
        }
        catch {
            $StopError = $_
            Write-Warning "LTX cleanup requires attention: $($_.Exception.Message)"
        }
    }

    $Measured = @($Runs | Where-Object { $_.generation -gt 1 })
    $Stats = $null
    $PerformancePass = $null
    $MeanTargetPass = $null
    if ($Measured.Count -eq 4) {
        $Stats = $Measured | Measure-Object -Property total_seconds -Average -Minimum -Maximum
        $PerformancePass = @($Measured | Where-Object { $_.total_seconds -gt 120 }).Count -eq 0
        $MeanTargetPass = $Stats.Average -ge 60 -and $Stats.Average -le 90
    }
    $Summary = [ordered]@{
        batch_id = $BatchId
        image = [string]$Definition.image
        prompt = $Prompt
        seed = $Seed
        input_audio_seconds = $AudioSeconds
        width = 1280
        height = 720
        fps = 24
        run_count = $Runs.Count
        measured_count = $Measured.Count
        mean_total_seconds = $(if ($null -ne $Stats) { $Stats.Average } else { $null })
        fastest_total_seconds = $(if ($null -ne $Stats) { $Stats.Minimum } else { $null })
        slowest_total_seconds = $(if ($null -ne $Stats) { $Stats.Maximum } else { $null })
        maximum_total_seconds = $(if ($null -ne $Stats) { $Stats.Maximum } else { $null })
        performance_pass = $PerformancePass
        mean_60_to_90_seconds = $MeanTargetPass
        quality_review = "pending_manual_lip_sync_and_facial_review"
        worker_stopped = $Stopped
        run_error = $(if ($null -ne $Failure) { $Failure.Exception.Message } else { $null })
        cleanup_error = $(if ($null -ne $StopError) { $StopError.Exception.Message } else { $null })
        runs = @($Runs.ToArray())
    }
    $SummaryFile = Join-Path $BatchDir "benchmark-summary.json"
    $Summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SummaryFile -Encoding utf8
    Write-Host "Benchmark report: $SummaryFile"
}
if ($null -ne $Failure) { throw $Failure }
if ($null -ne $StopError) { throw $StopError }
if (-not $PerformancePass) {
    throw "A warm generation exceeded 120 s; see benchmark-summary.json."
}
Write-Host "Warm benchmark completed; lip-sync/facial quality still requires visual review."
