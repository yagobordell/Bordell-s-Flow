[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Image,
    [Parameter(Mandatory)][string]$Prompt,
    [Parameter(Mandatory)][string]$ExpectedPinnedImage,
    [ValidateRange(0.5, 12)][double]$DurationSeconds = 2,
    [ValidateRange(0, 2147483646)][int]$SeedBase = 4242,
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
if ($ExpectedPinnedImage -notmatch "^[^\s@]+@sha256:[0-9a-fA-F]{64}$") {
    throw "-ExpectedPinnedImage must be an immutable repo@sha256:digest reference."
}
$Services = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\salad\services.json") -Raw | ConvertFrom-Json
$I2V = $Services.services.ltx25_i2v
$Avatar = $Services.services.ltx25
$ImageRepository = [string]$I2V.image -replace ":[^/:]+$", ""
if (-not $ExpectedPinnedImage.StartsWith("$ImageRepository@sha256:")) {
    throw "The expected image digest must refer to the reviewed I2V Docker repository."
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
& $Python $R2Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; refusing paid Salad capacity." }

$RunPath = Join-Path $OutputDir $RunId
$Inputs = Join-Path $RunPath "inputs"
$Output = Join-Path $RunPath "output"
& $Python $Smoke prepare --image $Image --prompt $Prompt --duration-seconds $DurationSeconds --output-dir $RunPath
if ($LASTEXITCODE -ne 0) { throw "I2V input preparation failed; no GPU allocated." }
$AvatarStatus = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive | Out-String)
if ($AvatarStatus -notmatch "status=(stopped|missing)(\s|$)") {
    throw "Avatar group must be stopped or missing during the isolated I2V smoke: $AvatarStatus"
}
$I2VStatus = (& $Manager -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive | Out-String)
if ($I2VStatus -notmatch "status=stopped(\s|$)" -or
    $I2VStatus -notmatch [regex]::Escape("image=$ExpectedPinnedImage")) {
    throw "Prepare the dedicated stopped I2V group with the reviewed immutable image first: $I2VStatus"
}
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
    & $Python $Runner --keyframes (Join-Path $Inputs "storyboard_keyframes.json") --prompts (Join-Path $Inputs "video_prompts.json") --timings (Join-Path $Inputs "shot_timings.json") --output-dir $Output --seed-base $SeedBase --timeout-seconds $TimeoutSeconds --poll-seconds 15
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
