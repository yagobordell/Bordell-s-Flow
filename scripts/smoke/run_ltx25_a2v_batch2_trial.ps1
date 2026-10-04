[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ImageA,
    [Parameter(Mandatory)][string]$AudioA,
    [Parameter(Mandatory)][string]$PromptA,
    [Parameter(Mandatory)][string]$ImageB,
    [Parameter(Mandatory)][string]$AudioB,
    [Parameter(Mandatory)][string]$PromptB,
    [long]$SeedA = 5243,
    [long]$SeedB = 5244,
    [Parameter(Mandatory)][string]$RunId,
    [Parameter(Mandatory)][string]$ExpectedPinnedImage,
    [string]$EnvFile = ".env",
    [string]$ReferenceA = "",
    [string]$ReferenceB = "",
    [ValidateSet("eager", "triton_experimental")][string]$VaeBackend = "eager",
    [ValidateSet(2, 4)][int]$GuidanceForwardBatchSize = 2,
    [ValidateRange(0, 1000000)][int]$MaxStage1MajorPageFaults = 0,
    [ValidateSet(1, 2, 4, 8)][int]$ProgressiveBlockGroupSize = 1,
    [ValidateRange(0, 10000)][double]$MinCheckpointDirectReadMiBps = 0,
    [ValidateRange(0, 3600)][double]$MaxStage1BuildSeconds = 0,
    [ValidateRange(0, 10800)][double]$PerformanceTargetSeconds = 0,
    [switch]$CheckInputsOnly,
    [switch]$AllowPaidTrial,
    [ValidateRange(120, 21600)][int]$BootstrapTimeoutSeconds = 7200,
    [switch]$NonInteractive
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$Probe = Join-Path $PSScriptRoot "verify_ltx25_a2v_batch2_image.py"
$Smoke = Join-Path $PSScriptRoot "submit_ltx25_a2v_batch2_smoke.py"
$Manager = Join-Path $Root "scripts\salad\manage_salad_worker.ps1"
$Readiness = Join-Path $PSScriptRoot "wait_salad_ltx25_ready.py"
$SourceCheck = Join-Path $PSScriptRoot "check_ltx25_python_source.py"
$Service = "ltx25_a2v_experimental"
$Destination = Join-Path $Root ("data\output\deployment-validation\ltx25-a2v-batch2\" + $RunId)
if ($ExpectedPinnedImage -notmatch '^[^\s@]+@sha256:[0-9a-fA-F]{64}$') {
    throw "Only immutable digest-pinned Docker images are permitted."
}
if ($CheckInputsOnly -and $AllowPaidTrial) {
    throw "Use either -CheckInputsOnly or -AllowPaidTrial, never both."
}
if (-not $CheckInputsOnly -and -not $AllowPaidTrial) {
    throw "First run -CheckInputsOnly. A paid trial requires explicit -AllowPaidTrial."
}
if ([bool]$ReferenceA -ne [bool]$ReferenceB) {
    throw "ReferenceA and ReferenceB must be supplied together."
}
if ($VaeBackend -eq "triton_experimental" -and -not $ReferenceA) {
    throw "Strict Triton requires exact approved Eager batch2 references for both clips."
}
if ($GuidanceForwardBatchSize -eq 4 -and -not $ReferenceA) {
    throw "Fused B4 guidance requires exact approved Eager/split-B2 references."
}

$ArgsList = @(
    "--image-a", $ImageA, "--audio-a", $AudioA, "--prompt-a", $PromptA,
    "--image-b", $ImageB, "--audio-b", $AudioB, "--prompt-b", $PromptB,
    "--seed-a", [string]$SeedA, "--seed-b", [string]$SeedB,
    "--run-id", $RunId, "--output-dir", $Destination,
    "--accept-full-hd-oom-risk", "--vae-backend", $VaeBackend,
    "--guidance-forward-batch-size", [string]$GuidanceForwardBatchSize,
    "--max-stage1-major-page-faults", [string]$MaxStage1MajorPageFaults,
    "--progressive-block-group-size", [string]$ProgressiveBlockGroupSize,
    "--min-checkpoint-direct-read-mibps", [string]$MinCheckpointDirectReadMiBps,
    "--max-stage1-build-seconds", [string]$MaxStage1BuildSeconds,
    "--performance-target-seconds", [string]$PerformanceTargetSeconds
)
if ($ReferenceA) { $ArgsList += @("--reference-a", $ReferenceA) }
if ($ReferenceB) { $ArgsList += @("--reference-b", $ReferenceB) }

& $Python $SourceCheck (Join-Path $Root "src\ai_video_factory\workers\ltx25\__init__.py")
if ($LASTEXITCODE -ne 0) { throw "Wrong Python source/worktree; synchronize uv first." }
& $Python $Smoke @ArgsList --check-inputs-only
if ($LASTEXITCODE -ne 0) { throw "A2V B2 local media/audio preflight FAILED. No GPU started." }
$ImageProbeArgs = @("--image", $ExpectedPinnedImage)
if ($VaeBackend -eq "triton_experimental") { $ImageProbeArgs += "--require-triton" }
& $Python $Probe @ImageProbeArgs
if ($LASTEXITCODE -ne 0) { throw "Docker ABI is invalid. No GPU started." }
if ($CheckInputsOnly) {
    Write-Host "A2V_B2_FREE_PREFLIGHT_OK -- no Salad GPU was started"
    return
}

. (Join-Path $Root "scripts\salad\_env_file.ps1")
Import-EnvFile -Path $EnvFile
$R2Probe = Join-Path $Root "scripts\pipeline\check_r2_ready.py"
& $Python $R2Probe
if ($LASTEXITCODE -ne 0) { throw "R2 unavailable; no GPU started." }
# The operator MUST first Prepare precisely this digest and check no other
# experimental capacity is running. Never override controller ownership.
$RemoteStatus = (& $Manager -Action Status -Service $Service -EnvFile $EnvFile -NonInteractive) -join " "
if ($RemoteStatus -notmatch "status=stopped" -or
    $RemoteStatus -notmatch "pending=False" -or
    $RemoteStatus -notmatch [regex]::Escape("image=$ExpectedPinnedImage")) {
    throw ("Refusing to start or stop an unowned/existing experimental group. " +
        "Prepare the exact digest and require status=stopped pending=False: " +
        $RemoteStatus)
}
$Started = $false
try {
    & $Manager -Action Start -Service $Service -Replicas 1 -EnvFile $EnvFile -AllowBootstrappingInstance -NonInteractive
    if (-not $?) {
        throw ("Isolated A2V experimental Start failed or returned ambiguous state. " +
            "Inspect Salad status before any manual recovery.")
    }
    # Only a confirmed successful Start grants this invocation cleanup ownership.
    # If remote Start failed ambiguously, do NOT stop someone else's group.
    $Started = $true
    $WaitArgs = @(
        "--service", $Service, "--env-file", $EnvFile,
        "--expected-image", $ExpectedPinnedImage,
        "--timeout-seconds", [string]$BootstrapTimeoutSeconds,
        "--poll-seconds", "15"
    )
    & $Python $Readiness @WaitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "A2V worker failed readiness / exact-image gate; nothing submitted."
    }
    & $Python $Smoke @ArgsList
    if ($LASTEXITCODE -ne 0) { throw "Paid A2V B2 inference/verification failed." }
}
finally {
    if ($Started) {
        & $Manager -Action Stop -Service $Service -EnvFile $EnvFile -NonInteractive
        if (-not $?) {
            throw "STOP failed; verify the experimental Salad group is stopped."
        }
    }
}
