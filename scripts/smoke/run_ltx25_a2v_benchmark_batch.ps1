[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Audio,
    [Parameter(Mandatory)][string]$AvatarImage,
    [Parameter(Mandatory)][string]$Prompt,
    [Parameter(Mandatory)][string]$ExpectedPinnedImage,
    [ValidateSet(
        "reference-bench-cpu", "reference-fp8none",
        "reference-cpu-triton", "reference-none-triton",
        "static-bench-cpu", "static-fp8none"
    )][string[]]$Profiles = @("reference-bench-cpu", "reference-fp8none"),
    [ValidateRange(3, 10)][int]$Repetitions = 4,
    [long]$Seed = 4242,
    [ValidateRange(0, 21600)][int]$MaxProfileTotalSeconds = 0,
    [switch]$ContinueOnTerminalFailure,
    [switch]$ReuseProductionGroup,
    [string]$OutputDir = "",
    [string]$EnvFile = ".env",
    [ValidateRange(120, 21600)][int]$BootstrapTimeoutSeconds = 7200,
    [switch]$NonInteractive
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# By default this runner uses only the separate experimental group.
# ReuseProductionGroup requires the guarded production-restore wrapper.
if ($ExpectedPinnedImage -notmatch '^[^\s@]+@sha256:[0-9a-fA-F]{64}$') {
    throw "ExpectedPinnedImage must be the reviewed immutable repository@sha256:digest"
}
if ($Seed -lt 0) { throw "Seed must be non-negative" }
if (-not (Test-Path -LiteralPath $Audio -PathType Leaf)) { throw "Missing audio: $Audio" }
if (-not (Test-Path -LiteralPath $AvatarImage -PathType Leaf)) {
    throw "Missing avatar: $AvatarImage"
}
if ([string]::IsNullOrWhiteSpace($Prompt)) { throw "Supply the same explicit prompt for every run" }
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$Manager = Join-Path $Root 'scripts\salad\manage_salad_worker.ps1'
$Isolation = Join-Path $PSScriptRoot 'check_ltx_a2v_benchmark_isolation.py'
$QueueGate = Join-Path $PSScriptRoot 'check_ltx_a2v_benchmark_jobs.py'
$Ready = Join-Path $PSScriptRoot 'wait_salad_ltx25_ready.py'
$Submit = Join-Path $PSScriptRoot 'submit_ltx25_a2v_smoke.py'
$SourceCheck = Join-Path $PSScriptRoot 'check_ltx25_python_source.py'
$Preflight = Join-Path $Root 'scripts\pipeline\check_r2_ready.py'
if ($ReuseProductionGroup -and $env:A2V_REUSE_WRAPPER_ACTIVE -ne "1") {
    throw "Production-group reuse must run through the guarded restore wrapper."
}
$TargetService = if ($ReuseProductionGroup) { 'ltx25' } else { 'ltx25_a2v_experimental' }
$QueueArgs = @()
$IsolationArgs = @('--env-file', $EnvFile)
if ($ReuseProductionGroup) {
    $QueueArgs += '--include-production'
    $IsolationArgs += @(
        '--reuse-production', '--expected-production-image', $ExpectedPinnedImage,
        '--expected-task-mode', 'a2v_experimental'
    )
}
$SourceModule = Join-Path $Root 'src\ai_video_factory\workers\ltx25\__init__.py'
& $Python $SourceCheck $SourceModule
if ($LASTEXITCODE -ne 0) { throw "Wrong Python worktree; run uv sync --locked --extra dev --python 3.12" }
& $Python $Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; no GPU allocated" }
& $Python $Isolation @IsolationArgs
if ($LASTEXITCODE -ne 0) { throw "A2V benchmark isolation preflight failed" }
& $Python $QueueGate @QueueArgs
if ($LASTEXITCODE -ne 0) { throw "A2V Postgres queue is not idle; no GPU allocated" }

$RunId = Get-Date -Format 'yyyyMMddHHmmss'
if (-not $OutputDir) {
    $OutputDir = Join-Path $Root "data\output\deployment-validation\ltx25-a2v-fp8-$RunId"
}
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$Manifest = [ordered]@{
    schema_version = 1
    git_head = (& git -C $Root rev-parse HEAD).Trim()
    pinned_image = $ExpectedPinnedImage
    audio_sha256 = (Get-FileHash -LiteralPath $Audio -Algorithm SHA256).Hash.ToLowerInvariant()
    avatar_sha256 = (Get-FileHash -LiteralPath $AvatarImage -Algorithm SHA256).Hash.ToLowerInvariant()
    seed = $Seed
    prompt = $Prompt
    profiles = $Profiles
    repetitions = $Repetitions
    runs = @()
    failures = @()
    skipped_profiles = @()
    lifecycle = [ordered]@{}
    salad_service = $TargetService
}
$Clock = [Diagnostics.Stopwatch]::StartNew()
$Started = $false
try {
    $startArgs = @{
        Action = 'Start'
        Service = $TargetService
        Replicas = 1
        EnvFile = $EnvFile
        AllowBootstrappingInstance = $true
    }
    if ($NonInteractive) { $startArgs.NonInteractive = $true }
    $Started = $true
    & $Manager @startArgs
    if (-not $?) { throw "A2V Salad allocation failed" }
    $Manifest.lifecycle.capacity_seconds = $Clock.Elapsed.TotalSeconds

    & $Python $Ready --env-file $EnvFile --timeout-seconds $BootstrapTimeoutSeconds --poll-seconds 15 --expected-image $ExpectedPinnedImage --service $TargetService
    if ($LASTEXITCODE -ne 0) { throw "Expected pinned A2V image never reached readiness" }
    $Manifest.lifecycle.ready_seconds = $Clock.Elapsed.TotalSeconds

    foreach ($profile in $Profiles) {
        for ($iteration = 1; $iteration -le $Repetitions; $iteration++) {
            # Recheck queues before each sample. Reuse mode blocks new production jobs.
            & $Python $QueueGate @QueueArgs
            if ($LASTEXITCODE -ne 0) {
                throw "A2V queue not idle before next sample; stopping experimental group"
            }
            $segment = "a2v-fp8-$RunId-$profile-$iteration"
            $out = Join-Path $OutputDir $segment
            $args = @(
                '--audio', $Audio, '--avatar-image', $AvatarImage,
                '--prompt', $Prompt, '--profile', $profile,
                '--seed', [string]$Seed, '--segment-id', $segment,
                '--output-dir', $out
            )
            Write-Host "Running $segment" -ForegroundColor Cyan
            $logPath = Join-Path $OutputDir "$segment.log"
            & $Python $Submit @args 2>&1 | Tee-Object -FilePath $logPath
            if ($LASTEXITCODE -ne 0) {
                $Failure = [ordered]@{
                    profile = $profile
                    iteration = $iteration
                    segment = $segment
                    log = $logPath
                    reason = 'submit_failed'
                }
                $Manifest.failures += $Failure
                $Manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $OutputDir 'manifest.json') -Encoding UTF8
                if (-not $ContinueOnTerminalFailure) {
                    throw "Benchmark failed: $segment; see $logPath"
                }
                # A timeout, retryable GPU OOM, active lease, queue contention
                # or local media-validation failure is NOT safe to skip. Only
                # a confirmed terminal 'failed' row with an idle A2V queue is.
                & $Python $QueueGate @QueueArgs --terminal-failure-dir $out --expected-segment $segment
                if ($LASTEXITCODE -ne 0) {
                    throw "Failed job is not safely terminal; stopping group: $segment"
                }
                Write-Warning "Skipping only terminally failed profile $profile; see $logPath"
                $Manifest.skipped_profiles += $profile
                break
            }
            $metadata = @(Get-ChildItem -LiteralPath $out -Filter 'metadata.json')
            $video = @(Get-ChildItem -LiteralPath $out -Filter '*.mp4')
            if ($metadata.Count -ne 1 -or $video.Count -ne 1) {
                throw "Missing or ambiguous benchmark artifacts for $segment"
            }
            $details = Get-Content -LiteralPath $metadata[0].FullName -Raw | ConvertFrom-Json
            if ($details.pipeline_reused -ne ($iteration -gt 1)) {
                throw "Incorrect cold/warm classification for $segment"
            }
            if ([bool]$details.prompt_cache_hit -ne [bool]($iteration -gt 1)) {
                throw "Gemma embedding cache failed to hit on the controlled warm prompt: $segment"
            }
            $Manifest.runs += [ordered]@{
                profile = $profile
                iteration = $iteration
                metadata = $metadata[0].FullName
                video = $video[0].FullName
                video_sha256 = (Get-FileHash -LiteralPath $video[0].FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                total_elapsed_seconds = $details.total_elapsed_seconds
                cuda_peak_reserved_mb = $details.cuda_peak_reserved_mb
            }
            $Manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $OutputDir 'manifest.json') -Encoding UTF8
            if ($MaxProfileTotalSeconds -gt 0 -and
                [double]$details.total_elapsed_seconds -gt $MaxProfileTotalSeconds) {
                Write-Warning "Profile $profile exceeded $MaxProfileTotalSeconds seconds; skipping remaining repetitions."
                $Manifest.skipped_profiles += $profile
                break
            }
        }
    }
    $Manifest.lifecycle.benchmark_seconds = $Clock.Elapsed.TotalSeconds
}
finally {
    # Always attempt cleanup; never touch the separate ltx25_i2v group.
    try {
        if ($Started) {
            & $Manager -Action Stop -Service $TargetService -EnvFile $EnvFile -NonInteractive
            if (-not $?) { throw "A2V Stop failed; manually verify stopped/no live instances/pending=False" }
        }
    }
    finally {
        $Manifest.lifecycle.end_to_end_seconds = $Clock.Elapsed.TotalSeconds
        $Manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $OutputDir 'manifest.json') -Encoding UTF8
        Write-Host "Review manifest: $(Join-Path $OutputDir 'manifest.json')"
    }
}
