# One pinned RTX 5090; compare sequential baseline with two simultaneous client requests.
# Deliberately does NOT claim simultaneous inference: the production worker serializes jobs.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ImageA,
    [Parameter(Mandatory)][string]$PromptA,
    [Parameter(Mandatory)][string]$ImageB,
    [Parameter(Mandatory)][string]$PromptB,
    [Parameter(Mandatory)][string]$ExpectedPinnedImage,
    [string]$RunId = ("i2v-queue-" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")),
    [ValidateRange(0.5,12)][double]$DurationSeconds = 5,
    [string]$EnvFile = ".env",
    [string]$OutputDir = "data/output/deployment-validation/ltx25-i2v-queue",
    [ValidateRange(120,21600)][int]$BootstrapTimeoutSeconds = 7200,
    [ValidateRange(60,43200)][int]$JobTimeoutSeconds = 10800
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Manager = Join-Path $Root "scripts\salad\manage_salad_worker.ps1"
$Ready = Join-Path $PSScriptRoot "wait_salad_ltx25_ready.py"
$Campaign = Join-Path $PSScriptRoot "ltx25_i2v_queue_campaign.py"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
if ($RunId -notmatch '^[A-Za-z0-9_-]{1,80}$') { throw "Use a fresh safe RunId." }
if ($ExpectedPinnedImage -notmatch '^[^\s@]+@sha256:[0-9a-fA-F]{64}$') { throw "Pin a reviewed I2V image digest." }
$RepoImage = (Get-Content (Join-Path $Root "deploy\salad\services.json") -Raw | ConvertFrom-Json).services.ltx25_i2v.image -replace ':[^/:]+$',''
if (-not $ExpectedPinnedImage.StartsWith("${RepoImage}@sha256:")) { throw "Wrong Docker repository for I2V." }
$Folder = Join-Path $Root (Join-Path $OutputDir $RunId)
$ArgsShared = @(
    "--image-a", $ImageA, "--prompt-a", $PromptA,
    "--image-b", $ImageB, "--prompt-b", $PromptB,
    "--output-dir", $Folder, "--duration-seconds", [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0}", $DurationSeconds),
    "--timeout-seconds", [string]$JobTimeoutSeconds
)
. (Join-Path $Root "scripts\salad\_env_file.ps1")
Import-EnvFile -Path $EnvFile
& $Python (Join-Path $PSScriptRoot "check_ltx25_python_source.py") (Join-Path $Root "src\ai_video_factory\workers\ltx25\__init__.py")
if ($LASTEXITCODE -ne 0) { throw "Wrong Python worktree." }
& $Python $Ready --service ltx25_i2v --env-file $EnvFile --expected-image $ExpectedPinnedImage --check-config-only
if ($LASTEXITCODE -ne 0) { throw "Invalid Salad account / digest configuration." }
& $Python (Join-Path $Root "scripts\pipeline\check_r2_ready.py")
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed." }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue) -or -not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffprobe and ffmpeg are required to validate generated videos."
}
# No Salad allocation until the inputs, prompt, geometry and output folders validate.
& $Python $Campaign --mode prepare @ArgsShared
if ($LASTEXITCODE -ne 0) { throw "I2V campaign prepare failed. No paid GPU allocated." }
$AvatarStatus = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive | Out-String)
$I2VStatus = (& $Manager -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive | Out-String)
if ($AvatarStatus -notmatch 'status=(stopped|missing)(\s|$)') {
    throw "A2V group must be stopped during the dedicated I2V campaign."
}
if ($I2VStatus -notmatch 'status=stopped(\s|$)' -or
    $I2VStatus -notmatch [regex]::Escape("image=$ExpectedPinnedImage")) {
    throw "I2V group must be prepared with the reviewed digest while stopped: $I2VStatus"
}
$Lifecycle = [ordered]@{
    purpose = "sequential baseline vs concurrent Postgres submission, NOT simultaneous GPU inference"
    image = $ExpectedPinnedImage
    run_id = $RunId
    started_utc = [DateTime]::UtcNow.ToString("o")
    bootstrap_seconds = $null
    stop_status = "pending"
}
$Started = $false
try {
    $Startup = [Diagnostics.Stopwatch]::StartNew()
    $Started = $true
    & $Manager -Action Start -Service ltx25_i2v -Replicas 1 -AllowBootstrappingInstance -EnvFile $EnvFile -NonInteractive
    if (-not $?) { throw "Failed to start exactly one I2V worker." }
    & $Python $Ready --service ltx25_i2v --env-file $EnvFile --expected-image $ExpectedPinnedImage --timeout-seconds $BootstrapTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "I2V readiness failed." }
    $Startup.Stop()
    $Lifecycle.bootstrap_seconds = $Startup.Elapsed.TotalSeconds
    & $Python $Campaign --mode run @ArgsShared
    if ($LASTEXITCODE -ne 0) { throw "Queue contention benchmark failed; inspect runner logs." }
}
finally {
    if ($Started) {
        try {
            & $Manager -Action Stop -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive
            if (-not $?) { throw "I2V cleanup failed." }
            $Lifecycle.stop_status = "stopped"
        } catch {
            $Lifecycle.stop_status = "FAILED: $_"
            throw
        } finally {
            [IO.File]::WriteAllText(
                (Join-Path $Folder "capacity-lifecycle.json"),
                ((ConvertTo-Json -InputObject $Lifecycle -Depth 5) + "`n"),
                [Text.UTF8Encoding]::new($false)
            )
        }
    }
}
Write-Host "I2V results: $(Join-Path $Folder 'campaign-results.json')."
Write-Warning "Two CLIENTS were concurrent. One LTX worker serializes GPU inference; no true parallel speedup is claimed."
