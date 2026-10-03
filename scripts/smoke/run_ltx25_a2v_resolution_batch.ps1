# Runs two DISTINCT avatars in one paid A2V Salad session: 720p, then experimental 1080p.
# No production routing changes; use a newly built, reviewed, digest-pinned branch image.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Avatar720,
    [Parameter(Mandatory)][string]$Audio720,
    [Parameter(Mandatory)][string]$Avatar1080,
    [Parameter(Mandatory)][string]$Audio1080,
    [Parameter(Mandatory)][string]$ExpectedPinnedImage,
    [Parameter(Mandatory)][switch]$Accept1080pOomRisk,
    [string]$Prompt = "A person speaking naturally to the camera, matching the supplied speech.",
    [string]$RunId = ("a2v-resolutions-" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")),
    [string]$EnvFile = ".env",
    [string]$OutputDir = "data/output/deployment-validation/ltx25-a2v-resolutions",
    [ValidateRange(120,21600)][int]$BootstrapTimeoutSeconds = 7200,
    [ValidateRange(60,43200)][int]$JobTimeoutSeconds = 10800
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Manager = Join-Path $Root "scripts\salad\manage_salad_worker.ps1"
$Ready = Join-Path $PSScriptRoot "wait_salad_ltx25_ready.py"
$Submit = Join-Path $PSScriptRoot "submit_ltx25_a2v_smoke.py"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
if ($RunId -notmatch '^[A-Za-z0-9_-]{1,80}$') { throw "Use a new safe RunId (1-80 characters)." }
if ($ExpectedPinnedImage -notmatch '^[^\s@]+@sha256:[0-9a-fA-F]{64}$') {
    throw "ExpectedPinnedImage must be the reviewed experimental repo@sha256:digest."
}
$RepoImage = (Get-Content (Join-Path $Root "deploy\salad\services.json") -Raw | ConvertFrom-Json).services.ltx25.image -replace ':[^/:]+$',''
if (-not $ExpectedPinnedImage.StartsWith("${RepoImage}@sha256:")) {
    throw "Pinned image repository does not match deploy/salad/services.json."
}
foreach ($File in @($Avatar720, $Audio720, $Avatar1080, $Audio1080)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "Missing test input: $File"
    }
}
if ((Get-FileHash $Avatar720 -Algorithm SHA256).Hash -eq (Get-FileHash $Avatar1080 -Algorithm SHA256).Hash) {
    throw "This campaign requires two DIFFERENT avatar images."
}
if ((Get-FileHash $Audio720 -Algorithm SHA256).Hash -eq (Get-FileHash $Audio1080 -Algorithm SHA256).Hash) {
    throw "This campaign requires two DIFFERENT speech files."
}
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { throw "ffprobe is required." }
foreach ($Audio in @($Audio720, $Audio1080)) {
    $DurationText = & ffprobe -v error -show_entries format=duration -of "default=noprint_wrappers=1:nokey=1" $Audio
    if ($LASTEXITCODE -ne 0) { throw "Could not probe audio: $Audio" }
    $Duration = [double]::Parse(([string]$DurationText).Trim(), [Globalization.CultureInfo]::InvariantCulture)
    if ($Duration -le 0 -or $Duration -gt 12) {
        throw "A2V reference speech must be 0-12 seconds. $Audio: $Duration s"
    }
}
. (Join-Path $Root "scripts\salad\_env_file.ps1")
Import-EnvFile -Path $EnvFile
& $Python (Join-Path $PSScriptRoot "check_ltx25_python_source.py") (Join-Path $Root "src\ai_video_factory\workers\ltx25\__init__.py")
if ($LASTEXITCODE -ne 0) { throw "Wrong Python checkout. Run uv sync --locked --extra dev --python 3.12." }
& $Python $Ready --service ltx25 --env-file $EnvFile --expected-image $ExpectedPinnedImage --check-config-only
if ($LASTEXITCODE -ne 0) { throw "Salad configuration preflight failed." }
& $Python (Join-Path $Root "scripts\pipeline\check_r2_ready.py")
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed." }
$AvatarGroup = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive | Out-String)
$I2VGroup = (& $Manager -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive | Out-String)
if ($AvatarGroup -notmatch 'status=stopped(\s|$)' -or
    $AvatarGroup -notmatch [regex]::Escape("image=$ExpectedPinnedImage")) {
    throw "Prepare the STOPPED A2V group with the new reviewed branch-image digest first: $AvatarGroup"
}
if ($I2VGroup -notmatch 'status=(stopped|missing)(\s|$)') {
    throw "I2V group must remain stopped: $I2VGroup"
}
$Folder = Join-Path $Root (Join-Path $OutputDir $RunId)
if (Test-Path -LiteralPath $Folder) { throw "RunId folder already exists: $Folder" }
New-Item -ItemType Directory -Force -Path $Folder | Out-Null
$SummaryPath = Join-Path $Folder "campaign.json"
$Summary = [ordered]@{
    run_id = $RunId
    pinned_image = $ExpectedPinnedImage
    session_started_utc = [DateTime]::UtcNow.ToString("o")
    bootstrap_seconds = $null
    results = @()
    cleanup = "pending"
}
function Save-Summary {
    [IO.File]::WriteAllText($SummaryPath, ((ConvertTo-Json -InputObject $Summary -Depth 12) + "`n"), [Text.UTF8Encoding]::new($false))
}
Save-Summary
$Started = $false
try {
    $Startup = [Diagnostics.Stopwatch]::StartNew()
    $Started = $true
    & $Manager -Action Start -Service ltx25 -Replicas 1 -AllowBootstrappingInstance -EnvFile $EnvFile -NonInteractive
    if (-not $?) { throw "Could not start A2V group." }
    & $Python $Ready --service ltx25 --env-file $EnvFile --expected-image $ExpectedPinnedImage --timeout-seconds $BootstrapTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "A2V group never became ready." }
    $Startup.Stop()
    $Summary.bootstrap_seconds = $Startup.Elapsed.TotalSeconds
    Save-Summary
    foreach ($Case in @(
        @{ name = "avatar-b-720p"; avatar = $Avatar720; audio = $Audio720; w = 1280; h = 720 },
        @{ name = "avatar-a-1080p"; avatar = $Avatar1080; audio = $Audio1080; w = 1920; h = 1080 }
    )) {
        $CaseName = [string]$Case.name
        $CaseFolder = Join-Path $Folder $CaseName
        New-Item -ItemType Directory -Force -Path $CaseFolder | Out-Null
        $Record = [ordered]@{
            case = $CaseName; width = $Case.w; height = $Case.h; status = "running"
            started_utc = [DateTime]::UtcNow.ToString("o"); client_wall_seconds = $null
            inference_seconds = $null; worker_total_seconds = $null; peak_vram_bytes = $null
            job_id = $null; replayed = $null; error = $null
        }
        $Summary.results += $Record
        Save-Summary
        $Wall = [Diagnostics.Stopwatch]::StartNew()
        try {
            $Cli = @("--avatar-image", [string]$Case.avatar, "--audio", [string]$Case.audio,
                "--profile", "reference", "--prompt", $Prompt, "--segment-id", "$RunId-$CaseName",
                "--width", [string]$Case.w, "--height", [string]$Case.h, "--fps", "24",
                "--seed", "4242", "--timeout-seconds", [string]$JobTimeoutSeconds,
                "--output-dir", $CaseFolder)
            & $Python $Submit @Cli
            if ($LASTEXITCODE -ne 0) { throw "$CaseName failed (exit=$LASTEXITCODE)." }
            $Metadata = Get-Content -LiteralPath (Join-Path $CaseFolder "metadata.json") -Raw | ConvertFrom-Json
            $Record.inference_seconds = $Metadata.inference_seconds
            $Record.worker_total_seconds = $Metadata.total_elapsed_seconds
            $Record.peak_vram_bytes = $Metadata.peak_vram_bytes
            $RequestFile = @(Get-ChildItem $CaseFolder -Filter "job-request-*.json")
            if ($RequestFile.Count -ne 1) { throw "Missing unique job request receipt." }
            $Record.job_id = (Get-Content -LiteralPath $RequestFile[0].FullName -Raw | ConvertFrom-Json).job_id
            $Record.status = "succeeded_visual_review_pending"
        }
        catch {
            $Record.status = "failed"
            $Record.error = [string]$_
            throw
        }
        finally {
            $Wall.Stop()
            $Record.client_wall_seconds = $Wall.Elapsed.TotalSeconds
            Save-Summary
        }
    }
}
finally {
    if ($Started) {
        try {
            & $Manager -Action Stop -Service ltx25 -EnvFile $EnvFile -NonInteractive
            if (-not $?) { throw "A2V stop failed." }
            $Summary.cleanup = "stopped"
        } catch {
            $Summary.cleanup = "FAILED: $_"
            throw
        } finally { Save-Summary }
    }
}
Write-Host "A2V campaign finished: $SummaryPath. Visually check lip-sync on BOTH MP4 files."
