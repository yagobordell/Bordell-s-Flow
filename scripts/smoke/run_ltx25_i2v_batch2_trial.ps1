# Experimental ONE-GPU paired FP8 I2V: never enabled for the normal Phase 8.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ImageA,
    [Parameter(Mandatory)][string]$PromptA,
    [Parameter(Mandatory)][string]$ImageB,
    [Parameter(Mandatory)][string]$PromptB,
    [Parameter(Mandatory)][string]$ExpectedPinnedImage,
    [string]$RunId = ("i2v-batch2-" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")),
    [int]$SeedA = 5243, [int]$SeedB = 5244,
    [ValidateSet(1280,1920)][int]$Width = 1280,
    [ValidateSet(720,1080)][int]$Height = 720,
    [switch]$AcceptFullHdBatch2OomRisk,
    [ValidateSet("cpu","disk")][string]$FullHdStage2Mode = "cpu",
    [ValidateSet("eager","triton_experimental")][string]$VaeBackend = "eager",
    [string]$ReferenceA = "", [string]$ReferenceB = "",
    [string]$EnvFile = ".env",
    [switch]$CheckInputsOnly, [switch]$AllowSequentialFallback,
    [switch]$DisableStageReuse, [switch]$EnableStageReuse,
    [ValidateRange(120,21600)][int]$BootstrapTimeoutSeconds = 7200,
    [ValidateRange(60,43200)][int]$JobTimeoutSeconds = 10800,
    [string]$OutputDir = "data/output/deployment-validation/ltx25-i2v-batch2"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$RepoRoot = $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
$Manager = Join-Path $Root "scripts\salad\manage_salad_worker.ps1"
$Ready = Join-Path $PSScriptRoot "wait_salad_ltx25_ready.py"
$Submit = Join-Path $PSScriptRoot "submit_ltx25_i2v_batch2_smoke.py"
if (($Width -eq 1920 -and $Height -ne 1080) -or
    ($Width -eq 1280 -and $Height -ne 720)) {
    throw "Only matching 1280x720 or 1920x1080 is supported."
}
if ($Width -eq 1920 -and -not $AcceptFullHdBatch2OomRisk) {
    throw "Full HD B2 is GPU-unverified; pass -AcceptFullHdBatch2OomRisk explicitly."
}
if ($Width -eq 1920 -and ($AllowSequentialFallback -or $EnableStageReuse)) {
    throw "Full HD trial must remain real B=2 with independent stage builds."
}
if ($VaeBackend -eq "triton_experimental" -and (
    $Width -ne 1920 -or $Height -ne 1080 -or $FullHdStage2Mode -ne "cpu" -or
    $AllowSequentialFallback -or $EnableStageReuse
)) {
    throw "Triton VAE experiment requires 1080p true B2 with CPU stage2."
}
if ($RunId -notmatch '^[A-Za-z0-9_-]{1,70}$') { throw "Use a new 1-70 character safe RunId." }
if ($ExpectedPinnedImage -notmatch '^[^\s@]+@sha256:[0-9a-fA-F]{64}$') {
    throw "ExpectedPinnedImage must be your newly BUILT experimental repo@sha256 digest."
}
$Manifest = Get-Content -LiteralPath (Join-Path $Root "deploy\salad\services.json") -Raw | ConvertFrom-Json
$RepoImage = $Manifest.services.ltx25_i2v.image -replace ':[^/:]+$',''
if (-not $ExpectedPinnedImage.StartsWith($RepoImage + "@sha256:")) {
    throw "Pinned repository does not match the I2V Salad manifest."
}
if ($Width -eq 1920 -and $FullHdStage2Mode -eq "cpu" -and
    [int64]$Manifest.services.ltx25_i2v.resources.memory -lt 61440) {
    throw "Full HD CPU streaming requires I2V memory >=61440 MB; host-RAM fit is still GPU-unvalidated."
}
if ([string]$Manifest.services.ltx25_i2v.environment.LTX_EXPERIMENTAL_I2V_BATCH2_ENABLED -ne "true") {
    throw "Wrong checkout: this experimental I2V manifest has no batch2 flag."
}
$Folder = Join-Path $Root (Join-Path $OutputDir $RunId)
$ArgsSubmit = @(
    "--image-a", $ImageA, "--prompt-a", $PromptA,
    "--image-b", $ImageB, "--prompt-b", $PromptB,
    "--seed-a", [string]$SeedA, "--seed-b", [string]$SeedB,
    "--width", [string]$Width, "--height", [string]$Height,
    "--full-hd-stage2-mode", $FullHdStage2Mode,
    "--vae-backend", $VaeBackend,
    "--run-id", $RunId, "--output-dir", $Folder,
    "--timeout-seconds", [string]$JobTimeoutSeconds
)
if ($ReferenceA) { $ArgsSubmit += @("--reference-a", $ReferenceA) }
if ($ReferenceB) { $ArgsSubmit += @("--reference-b", $ReferenceB) }
if ($AllowSequentialFallback) { $ArgsSubmit += "--allow-sequential-fallback" }
if ($DisableStageReuse) { $ArgsSubmit += "--disable-stage-reuse" }
if ($EnableStageReuse) { $ArgsSubmit += "--enable-stage-reuse" }
if ($AcceptFullHdBatch2OomRisk) { $ArgsSubmit += "--accept-full-hd-batch2-oom-risk" }
. (Join-Path $Root "scripts\salad\_env_file.ps1")
Import-EnvFile -Path $EnvFile
& $Python $Submit @ArgsSubmit --check-inputs-only
if ($LASTEXITCODE -ne 0) { throw "Local batch2 preflight failed; NO GPU was started." }
if ($CheckInputsOnly) {
    Write-Host "Local batch2 inputs and .env passed. No Salad/R2 operations occurred."
    return
}
& $Python (Join-Path $PSScriptRoot "check_ltx25_python_source.py") (Join-Path $Root "src\ai_video_factory\workers\ltx25\__init__.py")
if ($LASTEXITCODE -ne 0) { throw "Wrong Python checkout; run uv sync --locked --extra dev --python 3.12" }
& $Python $Ready --service ltx25_i2v --env-file $EnvFile --expected-image $ExpectedPinnedImage --check-config-only
if ($LASTEXITCODE -ne 0) { throw "Salad config/digest preflight failed." }
& $Python (Join-Path $Root "scripts\pipeline\check_r2_ready.py")
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed." }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue) -or
    -not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffprobe and ffmpeg are required."
}
if ($Width -eq 1920) {
    # A syntactically pinned digest does NOT prove its installed Python
    # schema contains the new hybrid field. This offline container check
    # blocks a costly Salad bootstrap with the wrong historical image.
    $ImageProbeArgs = @("--image", $ExpectedPinnedImage)
    if ($VaeBackend -eq "triton_experimental") { $ImageProbeArgs += "--require-triton" }
    & $Python (Join-Path $PSScriptRoot "verify_ltx25_i2v_hybrid_image.py") @ImageProbeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "STOP: pinned Docker lacks the hybrid I2V API; rebuild before paid Salad startup."
    }
}
$AvatarStatus = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive | Out-String)
$I2VStatus = (& $Manager -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive | Out-String)
if ($AvatarStatus -notmatch 'status=(stopped|missing)(\s|$)') {
    throw "A2V must be stopped before the batch2 experiment."
}
if ($I2VStatus -notmatch 'status=stopped(\s|$)' -or
    $I2VStatus -notmatch [regex]::Escape("image=$ExpectedPinnedImage") -or
    $I2VStatus -notmatch 'pending=False(\s|$)') {
    throw "Prepare STOPPED I2V with NEW experimental image first: $I2VStatus"
}
# Remote read-only proof complements the offline probe: both must refer
# to the identical immutable image digest, and the remote RAM must be real.
if ($Width -eq 1920) {
    & $Python (Join-Path $PSScriptRoot "verify_ltx25_i2v_remote_config.py") --image $ExpectedPinnedImage --env-file $EnvFile --minimum-memory-mb 61440
    if ($LASTEXITCODE -ne 0) {
        throw "STOP: remote I2V image, batch2 flag or RAM do not match the inspected Docker."
    }
}
if (Test-Path -LiteralPath $Folder) { throw "Run folder already exists: $Folder" }
$StartRequested = $false
$StartConfirmed = $false
$JobFailure = $null
$CleanupFailure = ""
try {
    $StartRequested = $true
    & $Manager -Action Start -Service ltx25_i2v -Replicas 1 -AllowBootstrappingInstance -EnvFile $EnvFile -NonInteractive
    if (-not $?) { throw "I2V start failed." }
    $StartConfirmed = $true
    & $Python $Ready --service ltx25_i2v --env-file $EnvFile --expected-image $ExpectedPinnedImage --timeout-seconds $BootstrapTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw "I2V instance did not become ready." }
    & $Python $Submit @ArgsSubmit
    if ($LASTEXITCODE -ne 0) { throw "Experimental batch2 job failed; no quality claims. Review Salad logs." }
}
catch {
    $JobFailure = $_
}
finally {
    if ($StartConfirmed) {
        # Do not override shared controller ownership if another operator took
        # over. A failed Stop MUST NOT mask the original inference failure.
        try {
            & $Manager -Action Stop -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive
            if (-not $?) { throw "Stop returned an unsuccessful PowerShell status." }
            $StoppedStatus = (& $Manager -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive | Out-String)
            if ($StoppedStatus -notmatch 'status=stopped(\s|$)' -or
                $StoppedStatus -notmatch 'pending=False(\s|$)') {
                throw "Stop did not prove a stably stopped remote group: $StoppedStatus"
            }
        }
        catch {
            $CleanupFailure = $_.Exception.Message
        }
    }
    elseif ($StartRequested) {
        # Start may have failed AFTER Salad accepted a mutation, so the state
        # is unknown. Never blindly Stop a group we have not established ours.
        $CleanupFailure = "Start did not confirm ownership; remote group state requires inspection."
    }
    if ($CleanupFailure) {
        Write-Warning "I2V CLEANUP UNVERIFIED: $CleanupFailure"
        Write-Warning "Check the I2V group and shared controller; do NOT override its lock or start another GPU."
        try {
            & $Manager -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive
        }
        catch {
            Write-Warning "I2V read-only Salad Status unavailable: $($_.Exception.Message)"
        }
        try {
            & $Python (Join-Path $Root "scripts\salad\inspect_capacity_lock.py")
            if ($LASTEXITCODE -ne 0) { Write-Warning "Capacity lock inspection unavailable." }
        }
        catch {
            Write-Warning "Capacity lock inspection unavailable: $($_.Exception.Message)"
        }
    }
}
if ($JobFailure) {
    if ($CleanupFailure) {
        Write-Warning "Original job error follows; cleanup requires separate operator action."
    }
    throw $JobFailure
}
if ($CleanupFailure) {
    throw "I2V experiment finished, but cleanup is UNVERIFIED: $CleanupFailure"
}
Write-Host "Batch2 experiment: $Folder"
Write-Warning "Compare BOTH outputs with B=1 references before using batching outside experiments."
