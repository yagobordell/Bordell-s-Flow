[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Image,
    [Parameter(Mandatory)][string]$Prompt,
    [Parameter(Mandatory)][string]$ExpectedPinnedImage,
    [ValidateRange(0.5, 12)][double]$DurationSeconds = 5,
    [ValidateRange(0, 2147483646)][int]$SeedBase = 4242,
    [ValidateSet("default", "cfg-b2", "cfg-b2-silent", "fast-vanilla-reference", "fast-vanilla", "fast-lowres", "fast-lowres-triton", "fast-lowres-none", "fast-vanilla-none", "fast-vanilla-triton", "fast-vanilla-nvfp4")][string[]]$ProfileSelection = @("default", "cfg-b2", "fast-vanilla-reference", "fast-vanilla", "fast-lowres"),
    [ValidateRange(1, 3)][int]$RepeatCount = 1,
    [switch]$AllowNoOffload,
    [string]$BaselineRunId = "",
    [string]$BaselineLogsCsv = "",
    [string]$RunId = ("fast-batch-" + [Guid]::NewGuid().ToString("N")),
    [string]$OutputDir = "data/output/deployment-validation/ltx25-i2v-fast-batches",
    [string]$EnvFile = ".env",
    [ValidateRange(120, 21600)][int]$BootstrapTimeoutSeconds = 7200,
    [ValidateRange(60, 43200)][int]$TimeoutSeconds = 10800,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Manager = Join-Path $RepoRoot "scripts\salad\manage_salad_worker.ps1"
$SourceCheck = Join-Path $PSScriptRoot "check_ltx25_python_source.py"
$Ready = Join-Path $PSScriptRoot "wait_salad_ltx25_ready.py"
$Smoke = Join-Path $PSScriptRoot "ltx25_i2v_smoke.py"
$R2Preflight = Join-Path $RepoRoot "scripts\pipeline\check_r2_ready.py"
$Runner = Join-Path $RepoRoot "scripts\pipeline\run_phase8_videos.py"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if ($RunId -notmatch "^[A-Za-z0-9_-]{1,80}$") {
    throw "RunId must contain only letters, numbers, underscore or hyphen (1-80 chars)."
}
if ($BaselineRunId) { throw "The fast benchmark always performs NEW GPU inference; omit historical BaselineRunId." }
if ($ProfileSelection -contains "fast-vanilla-nvfp4" -and
    $ProfileSelection.Count -ne 1) {
    throw "Native NVFP4 requires its own exclusive pinned Docker image and separate benchmark run."
}
if (($ProfileSelection -contains "fast-lowres-none" -or
     $ProfileSelection -contains "fast-vanilla-none") -and -not $AllowNoOffload) {
    throw "FP8 no-offload may OOM on a 32 GB RTX 5090; pass -AllowNoOffload explicitly."
}
if (@($ProfileSelection | Select-Object -Unique).Count -ne $ProfileSelection.Count) {
    throw "Use -RepeatCount instead of duplicate profile names."
}
if ($BaselineRunId -and $BaselineRunId -notmatch "^[A-Za-z0-9_-]{1,80}$") {
    throw "Invalid BaselineRunId."
}
if ([bool]$BaselineRunId -ne [bool]$BaselineLogsCsv) {
    throw "Provide both -BaselineRunId and -BaselineLogsCsv, or neither."
}
if ($BaselineLogsCsv -and -not (Test-Path -LiteralPath $BaselineLogsCsv -PathType Leaf)) {
    throw "Cannot find original baseline Salad CSV: $BaselineLogsCsv"
}
if ($ExpectedPinnedImage -notmatch "^[^\s@]+@sha256:[0-9a-fA-F]{64}$") {
    throw "-ExpectedPinnedImage must be an immutable repo@sha256:digest reference."
}
$Services = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\salad\services.json") -Raw | ConvertFrom-Json
$I2V = $Services.services.ltx25_i2v
$Avatar = $Services.services.ltx25
$ImageRepository = [string]$I2V.image -replace ":[^/:]+$", ""
if (-not $ExpectedPinnedImage.StartsWith("${ImageRepository}@sha256:")) {
    throw "The expected image digest must refer to the reviewed I2V Docker repository."
}
if ($ExpectedPinnedImage -eq "docker.io/yagobordell/ai-video-factory@sha256:bffdca9cfaf5e6e19cd9f20b7f88a562c3af8cf2854d7bad56e1f66700a87564") {
    throw "The v11 image from i2v-smoke-003 does not contain static-camera inference; publish a NEW image first."
}
if ($I2V.group_name -eq $Avatar.group_name -or
    $I2V.environment.LTX_WORKER_TASK_MODE -ne "i2v" -or
    $Avatar.environment.LTX_WORKER_TASK_MODE -ne "a2v") {
    throw "Salad manifest does not isolate I2V from avatar tasks and groups."
}
if (-not (Test-Path -LiteralPath $Image -PathType Leaf)) {
    throw "I2V input image does not exist: $Image"
}
. (Join-Path $RepoRoot "scripts\salad\_env_file.ps1")
Import-EnvFile -Path $EnvFile
& $Python $SourceCheck (Join-Path $RepoRoot "src\ai_video_factory\workers\ltx25\__init__.py")
if ($LASTEXITCODE -ne 0) { throw "LTX Python source mismatch; run uv sync --locked --extra dev --python 3.12." }
# Fail locally before allocating Salad if readiness credentials or account selection drift.
& $Python $Ready --service ltx25_i2v --env-file $EnvFile --expected-image $ExpectedPinnedImage --check-config-only
if ($LASTEXITCODE -ne 0) { throw "Local I2V readiness configuration preflight failed; no GPU allocated." }
& $Python $R2Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; refusing paid Salad capacity." }


# Prepare and hash-check every candidate BEFORE allocating paid capacity.
$BatchPath = Join-Path $OutputDir $RunId
if (Test-Path -LiteralPath $BatchPath) {
    throw "Batch directory already exists; use a new -RunId: $BatchPath"
}
$Profiles = @($ProfileSelection)
if ($Profiles -contains "fast-vanilla-reference" -and
    $Profiles -contains "fast-vanilla" -and
    [Array]::IndexOf($Profiles, "fast-vanilla-reference") -gt
    [Array]::IndexOf($Profiles, "fast-vanilla")) {
    throw "The upstream vanilla reference must precede instrumented vanilla."
}
if (($Profiles -contains "fast-lowres-none" -or
     $Profiles -contains "fast-vanilla-none") -and
    $Profiles[-1] -notin @("fast-lowres-none", "fast-vanilla-none")) {
    throw "Potential OOM: a no-offload FP8 profile must be LAST in the batch."
}
$Collector = Join-Path $PSScriptRoot "ltx25_i2v_benchmark_metrics.py"
function Save-BatchJson {
    param([string]$Path, $Value)
    $Json = ConvertTo-Json -InputObject $Value -Depth 16
    [IO.File]::WriteAllText([IO.Path]::GetFullPath($Path), $Json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}
function Require-Python {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit=$LASTEXITCODE)." }
}
function Assert-SameInputs {
    param([string]$Reference, [string]$Candidate)
    foreach ($Name in @("keyframe.png", "video_prompts.json", "shot_timings.json")) {
        $A = Join-Path $Reference $Name
        $B = Join-Path $Candidate $Name
        if (-not (Test-Path -LiteralPath $A -PathType Leaf) -or
            -not (Test-Path -LiteralPath $B -PathType Leaf) -or
            (Get-FileHash -LiteralPath $A -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $B -Algorithm SHA256).Hash) {
            throw "Benchmark input mismatch: $Name. No paid GPU will be allocated."
        }
    }
}
$BaselinePath = ""
if ($BaselineRunId) {
    $BaselinePath = Join-Path $RepoRoot "data\output\deployment-validation\ltx25-i2v\$BaselineRunId"
    $OldReportFile = Join-Path $BaselinePath "i2v-smoke-report.json"
    $OldVideo = Join-Path $BaselinePath "output\video_clips\shot_001.mp4"
    $OldManifestFile = Join-Path $BaselinePath "output\video_generation_manifest.json"
    foreach ($File in @($OldReportFile, $OldVideo, $OldManifestFile)) {
        if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
            throw "Verified original baseline file is missing: $File"
        }
    }
    $OldReport = Get-Content -LiteralPath $OldReportFile -Raw | ConvertFrom-Json
    $OldManifest = Get-Content -LiteralPath $OldManifestFile -Raw | ConvertFrom-Json
    if ($OldReport.status -ne "technically_valid_visual_review_pending" -or
        [Math]::Abs([double]$OldReport.requested_duration_seconds - $DurationSeconds) -gt 0.000001 -or
        (Get-FileHash -LiteralPath $OldVideo -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$OldReport.video_sha256 -or
        $OldManifest.jobs.Count -ne 1 -or
        $OldManifest.jobs[0].transport_status -ne "succeeded" -or
        $OldManifest.jobs[0].response.replayed -eq $true) {
        throw "Original baseline report, MP4 or Postgres manifest failed validation."
    }
}
$ReferenceInputs = ""
foreach ($Round in 1..$RepeatCount) {
    foreach ($Profile in $Profiles) {
        $Key = "r$Round-$Profile"
        $ProfilePath = Join-Path $BatchPath $Key
        & $Python $Smoke prepare --image $Image --prompt $Prompt --duration-seconds $DurationSeconds --output-dir $ProfilePath
        Require-Python "Local input preparation for $Key"
        $Candidate = Join-Path $ProfilePath "inputs"
        if ($ReferenceInputs) { Assert-SameInputs $ReferenceInputs $Candidate }
        else { $ReferenceInputs = $Candidate }
    }
}
$Hasher = [Security.Cryptography.SHA256]::Create()
try {
    $SeedIdentity = if ($BaselineRunId) { $BaselineRunId } else { $RunId }
    $RunSeedHash = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($SeedIdentity))
}
finally {
    $Hasher.Dispose()
}
$RunIdOffset = [BitConverter]::ToUInt32($RunSeedHash, 0)
$EffectiveSeedBase = [int](([long]$SeedBase + [long]$RunIdOffset) % 2147483646)
$Plan = [ordered]@{
    schema_version = 1
    batch_id = $RunId
    pinned_image = $ExpectedPinnedImage
    profiles = @($Profiles)
    repeat_count = $RepeatCount
    original_baseline_run_id = $BaselineRunId
    effective_seed_base = $EffectiveSeedBase
    duration_seconds = $DurationSeconds
    bootstrap_seconds = $null
    bootstrapped_at_utc = $null
    stopped_at_utc = $null
}
Save-BatchJson (Join-Path $BatchPath "batch-manifest.json") $Plan

if ($BaselineRunId) {
    $Imported = Join-Path $BatchPath "baseline"
    New-Item -ItemType Directory -Path $Imported -Force | Out-Null
    & $Python $Collector from-csv --profile default --csv $BaselineLogsCsv --job-id $OldReport.application_job_id --output (Join-Path $Imported "phase-metrics.json")
    Require-Python "Import of real original baseline timings"
    Save-BatchJson (Join-Path $Imported "run-result.json") ([ordered]@{
        profile = "default"
        status = "succeeded"
        run_id = $BaselineRunId
        job_id = $OldReport.application_job_id
        client_wall_seconds = $null
        gpu_run_start_utc = $null
        gpu_run_end_utc = $null
        video_sha256 = $OldReport.video_sha256
        original_video_path = $OldVideo
    })
    & $Python $Collector summarize --batch-dir $BatchPath
    Require-Python "Initial baseline summary"
}
$AvatarStatus = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive | Out-String)
if ($AvatarStatus -notmatch "status=(stopped|missing)(\s|$)") {
    throw "A2V group must be stopped before the exclusive I2V batch: $AvatarStatus"
}
$I2VStatus = (& $Manager -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive | Out-String)
if ($I2VStatus -notmatch "status=stopped(\s|$)" -or
    $I2VStatus -notmatch [regex]::Escape("image=$ExpectedPinnedImage")) {
    throw "Prepare the stopped I2V group with this immutable image first: $I2VStatus"
}
Write-Host "STARTING ONE Salad GPU for $($Profiles.Count * $RepeatCount) real I2V trials." -ForegroundColor Cyan
$StartRequested = $false
try {
    $StartRequested = $true
    $BootstrapClock = [Diagnostics.Stopwatch]::StartNew()
    & $Manager -Action Start -Service ltx25_i2v -Replicas 1 -AllowBootstrappingInstance -EnvFile $EnvFile -NonInteractive
    if (-not $?) { throw "Salad GPU startup failed." }
    & $Python $Ready --service ltx25_i2v --env-file $EnvFile --expected-image $ExpectedPinnedImage --timeout-seconds $BootstrapTimeoutSeconds
    Require-Python "One-time worker /ready gate"
    $BootstrapClock.Stop()
    $Plan.bootstrap_seconds = $BootstrapClock.Elapsed.TotalSeconds
    $Plan.bootstrapped_at_utc = [DateTime]::UtcNow.ToString("o")
    Save-BatchJson (Join-Path $BatchPath "batch-manifest.json") $Plan
    foreach ($Round in 1..$RepeatCount) {
    foreach ($Profile in $Profiles) {
        $RoundSeedBase = [int](([long]$EffectiveSeedBase + (($Round - 1) * 10000L)) % 2147483646)
        $Key = "r$Round-$Profile"
        $AvatarStatus = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive | Out-String)
        if ($AvatarStatus -notmatch "status=(stopped|missing)(\s|$)") {
            throw "The A2V group started during the exclusive I2V benchmark."
        }
        $ProfilePath = Join-Path $BatchPath $Key
        $Inputs = Join-Path $ProfilePath "inputs"
        $Output = Join-Path $ProfilePath "output"
        $StartUtc = [DateTime]::UtcNow
        $Timer = [Diagnostics.Stopwatch]::StartNew()
        $Result = [ordered]@{
            profile = $Profile
            repeat_index = $Round
            effective_seed_base = $RoundSeedBase
            status = "running"
            run_id = $RunId
            job_id = $null
            client_wall_seconds = $null
            gpu_run_start_utc = $StartUtc.ToString("o")
            gpu_run_end_utc = $null
            video_sha256 = $null
        }
        Save-BatchJson (Join-Path $ProfilePath "run-result.json") $Result
        Write-Host "RUNNING $Key seed=$RoundSeedBase; same paid GPU stays running." -ForegroundColor Cyan
        try {
            $RunnerArgs = @(
                "--keyframes", (Join-Path $Inputs "storyboard_keyframes.json"),
                "--prompts", (Join-Path $Inputs "video_prompts.json"),
                "--timings", (Join-Path $Inputs "shot_timings.json"),
                "--output-dir", $Output,
                "--seed-base", $RoundSeedBase,
                "--timeout-seconds", $TimeoutSeconds,
                "--poll-seconds", 15,
                "--no-retry-terminal",
                "--i2v-profile", $Profile
            )
            & $Python $Runner @RunnerArgs
            Require-Python "GPU generation: $Profile"
            $Timer.Stop()
            $EndUtc = [DateTime]::UtcNow
            $Result.gpu_run_end_utc = $EndUtc.ToString("o")
            $Result.client_wall_seconds = $Timer.Elapsed.TotalSeconds
            $JobManifest = Get-Content -LiteralPath (Join-Path $Output "video_generation_manifest.json") -Raw | ConvertFrom-Json
            if ($JobManifest.jobs.Count -ne 1 -or
                $JobManifest.jobs[0].transport_status -ne "succeeded" -or
                $JobManifest.jobs[0].response.replayed -eq $true) {
                throw "Unexpected Postgres result or cached replay for $Profile"
            }
            $Result.job_id = [string]$JobManifest.jobs[0].application_job_id
            & $Python $Smoke validate --video (Join-Path $Output "video_clips\shot_001.mp4") --duration-seconds $DurationSeconds --output-dir $ProfilePath
            Require-Python "MP4 validation: $Profile"
            $Technical = Get-Content -LiteralPath (Join-Path $ProfilePath "i2v-smoke-report.json") -Raw | ConvertFrom-Json
            $Result.video_sha256 = [string]$Technical.video_sha256
            if ($Profile -eq "fast-vanilla" -and
                $Profiles -contains "fast-vanilla-reference") {
                $ReferenceReportPath = Join-Path $BatchPath (
                    "r$Round-fast-vanilla-reference\i2v-smoke-report.json"
                )
                if (-not (Test-Path -LiteralPath $ReferenceReportPath -PathType Leaf)) {
                    throw "Missing same-seed upstream vanilla reference receipt."
                }
                $ReferenceReport = Get-Content -LiteralPath $ReferenceReportPath -Raw |
                    ConvertFrom-Json
                $ExactMatch = [string]$ReferenceReport.video_sha256 -ceq $Result.video_sha256
                $Equivalence = [ordered]@{
                    round = $Round
                    seed_base = $RoundSeedBase
                    upstream_sha256 = [string]$ReferenceReport.video_sha256
                    instrumented_sha256 = $Result.video_sha256
                    exact_mp4_sha256_match = $ExactMatch
                }
                Save-BatchJson (Join-Path $ProfilePath "vanilla-equivalence.json") $Equivalence
                if (-not $ExactMatch) {
                    throw "VANILLA QUALITY GATE FAILED: instrumented vanilla MP4 differs from the same-seed pinned upstream vanilla. Do not promote FAST."
                }
                Write-Host "VANILLA QUALITY GATE PASSED: reference and silent instrumented MP4 identical." -ForegroundColor Green
            }
            $Result.status = "video_valid_metrics_pending"
            Save-BatchJson (Join-Path $ProfilePath "run-result.json") $Result
            $LogsEndUtc = [DateTime]::UtcNow
            & $Python $Collector collect --profile $Profile --job-id $Result.job_id --env-file $EnvFile --start-utc $StartUtc.ToString("o") --end-utc $LogsEndUtc.ToString("o") --output (Join-Path $ProfilePath "phase-metrics.json")
            Require-Python "Complete Salad per-phase metrics: $Profile"
            $Result.status = "succeeded"
            Save-BatchJson (Join-Path $ProfilePath "run-result.json") $Result
            & $Python $Collector summarize --batch-dir $BatchPath
            Require-Python "CSV summary: $Profile"
            Write-Host "SAVED complete times and validated MP4: $Profile" -ForegroundColor Green
        }
        catch {
            $Timer.Stop()
            if (-not $Result.gpu_run_end_utc) {
                $Result.gpu_run_end_utc = [DateTime]::UtcNow.ToString("o")
                $Result.client_wall_seconds = $Timer.Elapsed.TotalSeconds
            }
            if ($Result.status -ne "succeeded") { $Result.status = "failed" }
            $Result["error"] = $_.Exception.Message
            Save-BatchJson (Join-Path $ProfilePath "run-result.json") $Result
            & $Python $Collector summarize --batch-dir $BatchPath
            throw
        }
    }
    }
}
finally {
    if ($StartRequested) {
        Write-Host "STOPPING I2V Salad capacity after this batch." -ForegroundColor Yellow
        & $Manager -Action Stop -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive
        if (-not $?) { throw "I2V cleanup FAILED: manually verify Salad capacity is OFF." }
        $Plan.stopped_at_utc = [DateTime]::UtcNow.ToString("o")
        Save-BatchJson (Join-Path $BatchPath "batch-manifest.json") $Plan
    }
}
Write-Host "I2V batch DONE: $(Join-Path $BatchPath 'summary.csv')" -ForegroundColor Green
