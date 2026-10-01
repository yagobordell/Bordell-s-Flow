[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Image,
    [Parameter(Mandatory)][string]$Prompt,
    [Parameter(Mandatory)][string]$ExpectedPinnedImage,
    [ValidateRange(0.5, 12)][double]$DurationSeconds = 2,
    [ValidateRange(0, 2147483646)][int]$SeedBase = 4242,
    [ValidateSet("default", "static-camera", "cfg-b2", "cfg-b2-silent", "cfg-b2-triton")][string]$I2VProfile = "default",
    [string]$CompareWithRunId = "",
    [string]$RunId = ([Guid]::NewGuid().ToString("N")),
    [string]$OutputDir = "data/output/deployment-validation/ltx25-i2v",
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
if (-not [string]::IsNullOrWhiteSpace($CompareWithRunId) -and
    ($CompareWithRunId -notmatch "^[A-Za-z0-9_-]{1,80}$" -or $CompareWithRunId -eq $RunId)) {
    throw "-CompareWithRunId must be a different, existing safe baseline RunId."
}
if (-not [string]::IsNullOrWhiteSpace($CompareWithRunId) -and $I2VProfile -eq "default") {
    throw "-CompareWithRunId requires an experimental I2V profile."
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
if ($I2VProfile -ne "default" -and
    $ExpectedPinnedImage -eq "docker.io/yagobordell/ai-video-factory@sha256:bffdca9cfaf5e6e19cd9f20b7f88a562c3af8cf2854d7bad56e1f66700a87564") {
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

$RunPath = Join-Path $OutputDir $RunId
$Inputs = Join-Path $RunPath "inputs"
$Output = Join-Path $RunPath "output"
& $Python $Smoke prepare --image $Image --prompt $Prompt --duration-seconds $DurationSeconds --output-dir $RunPath
if ($LASTEXITCODE -ne 0) { throw "I2V input preparation failed; no GPU allocated." }
# Verify comparable inputs before allocating a paid GPU.
if (-not [string]::IsNullOrWhiteSpace($CompareWithRunId)) {
    $BaselinePath = Join-Path $OutputDir $CompareWithRunId
    $BaselineReport = Join-Path $BaselinePath "i2v-smoke-report.json"
    $BaselineVideo = Join-Path $BaselinePath "output\video_clips\shot_001.mp4"
    if (-not (Test-Path -LiteralPath $BaselineReport -PathType Leaf) -or
        -not (Test-Path -LiteralPath $BaselineVideo -PathType Leaf)) {
        throw "Comparison baseline must contain a technically validated MP4: $BaselinePath"
    }
    $BaselineCheck = Get-Content -LiteralPath $BaselineReport -Raw | ConvertFrom-Json
    if ($BaselineCheck.status -ne "technically_valid_visual_review_pending" -or
        (Get-FileHash -LiteralPath $BaselineVideo -Algorithm SHA256).Hash.ToLowerInvariant() -ne
        [string]$BaselineCheck.video_sha256) {
        throw "Comparison baseline MP4 failed report/hash validation."
    }
    $BaselineInputs = Join-Path $BaselinePath "inputs"
    $BaselineImage = Join-Path $BaselineInputs "keyframe.png"
    if (-not (Test-Path -LiteralPath $BaselineImage -PathType Leaf) -or
        (Get-FileHash -LiteralPath $BaselineImage -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath (Join-Path $Inputs "keyframe.png") -Algorithm SHA256).Hash) {
        throw "Comparison baseline keyframe differs from this run."
    }
    foreach ($Name in @("video_prompts.json", "shot_timings.json")) {
        $Old = Join-Path $BaselineInputs $Name
        $New = Join-Path $Inputs $Name
        if (-not (Test-Path -LiteralPath $Old -PathType Leaf) -or
            (Get-Content -LiteralPath $Old -Raw) -cne (Get-Content -LiteralPath $New -Raw)) {
            throw "Comparison baseline $Name differs from this run."
        }
    }
    Write-Host "I2V baseline preflight OK: $CompareWithRunId (same keyframe, prompt and duration)"
}
$AvatarStatus = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive | Out-String)
if ($AvatarStatus -notmatch "status=(stopped|missing)(\s|$)") {
    throw "Avatar group must be stopped or missing during the isolated I2V smoke: $AvatarStatus"
}
$I2VStatus = (& $Manager -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive | Out-String)
if ($I2VStatus -notmatch "status=stopped(\s|$)" -or
    $I2VStatus -notmatch [regex]::Escape("image=$ExpectedPinnedImage")) {
    throw "Prepare the dedicated stopped I2V group with the reviewed immutable image first: $I2VStatus"
}
$Hasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $SeedIdentity = if ([string]::IsNullOrWhiteSpace($CompareWithRunId)) { $RunId } else { $CompareWithRunId }
    $RunSeedHash = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($SeedIdentity))
}
finally {
    $Hasher.Dispose()
}
$RunIdOffset = [BitConverter]::ToUInt32($RunSeedHash, 0)
$EffectiveSeedBase = [int](([long]$SeedBase + [long]$RunIdOffset) % 2147483646)
Write-Host "I2V run_id=$RunId profile=$I2VProfile seed_identity=$SeedIdentity seed_base=$EffectiveSeedBase"
$StartRequested = $false
try {
    $StartRequested = $true
    & $Manager -Action Start -Service ltx25_i2v -Replicas 1 -AllowBootstrappingInstance -EnvFile $EnvFile -NonInteractive
    if (-not $?) { throw "I2V Salad capacity start failed." }
    & $Python $Ready --service ltx25_i2v --env-file $EnvFile --expected-image $ExpectedPinnedImage --timeout-seconds $BootstrapTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "Dedicated I2V /ready gate failed: job NOT submitted." }
    $AvatarStatus = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive | Out-String)
    if ($AvatarStatus -notmatch "status=(stopped|missing)(\s|$)") {
        throw "Avatar group became active; refusing overlapping I2V job."
    }
    $RunnerArgs = @(
        "--keyframes", (Join-Path $Inputs "storyboard_keyframes.json"),
        "--prompts", (Join-Path $Inputs "video_prompts.json"),
        "--timings", (Join-Path $Inputs "shot_timings.json"),
        "--output-dir", $Output,
        "--seed-base", $EffectiveSeedBase,
        "--timeout-seconds", $TimeoutSeconds,
        "--poll-seconds", 15
    )
    if ($I2VProfile -ne "default") { $RunnerArgs += @("--i2v-profile", $I2VProfile) }
    & $Python $Runner @RunnerArgs
    if ($LASTEXITCODE -ne 0) { throw "I2V Postgres/R2 generation failed." }
    & $Python $Smoke validate --video (Join-Path $Output "video_clips\shot_001.mp4") --duration-seconds $DurationSeconds --output-dir $RunPath
    if ($LASTEXITCODE -ne 0) { throw "I2V output technical verification failed." }
}
finally {
    if ($StartRequested) {
        & $Manager -Action Stop -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive
        if (-not $?) { throw "I2V cleanup failed: verify Salad group stopped and replica billing." }
    }
}
Write-Host "I2V technical smoke completed. Review the MP4 visually before accepting this PR: $RunPath" -ForegroundColor Green
