[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExperimentalTag,
    [Parameter(Mandatory)][string]$ExperimentalPinnedImage,
    [ValidateRange(120, 7200)][int]$RaceTimeoutSeconds = 5400,
    [ValidateSet(1, 2)][int]$StartupReplicas = 2,
    [string]$EnvFile = ".env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
Set-Location $RepoRoot

$Manager = Join-Path $RepoRoot "scripts\salad\manage_salad_worker.ps1"
$Smoke = Join-Path $PSScriptRoot "run_ltx25_a2v_smoke_controlled.ps1"
$Services = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\salad\services.json") -Raw | ConvertFrom-Json
$OriginalTag = [string]$Services.services.ltx25.image
$AuxGroupName = "$($Services.services.ltx25.group_name)-race"
$OriginalPinnedImage = "docker.io/yagobordell/ai-video-factory@sha256:3a46d675c4ca93ea065c0540ba38efdb7a45cf447cecb841524aaee0d7342469"

function Get-LtxStatus {
    param([switch]$IsolatedRaceGroup)
    $ExpectedGroup = if ($IsolatedRaceGroup) {
        $AuxGroupName
    } else {
        [string]$Services.services.ltx25.group_name
    }
    # Read-only preflight may hit a transient API/network failure. Do not
    # start or prepare either group until this explicit query succeeds.
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        $Output = @()
        $ExitCode = -1
        $Failure = ""
        try {
            # Do not splat a dynamically constructed switch into a nested
            # powershell.exe call: use the same literal args as manual Status.
            if ($IsolatedRaceGroup) {
                $Output = @(
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Status -Service ltx25 -IsolatedRaceGroup -EnvFile $EnvFile -NonInteractive 2>&1
                )
            }
            else {
                $Output = @(
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive 2>&1
                )
            }
            $ExitCode = [int]$LASTEXITCODE
        }
        catch {
            $Failure = [string]$_.Exception.Message
            if ($null -ne $LASTEXITCODE) { $ExitCode = [int]$LASTEXITCODE }
        }
        $StatusText = (@($Output | ForEach-Object { [string]$_ }) -join " ").Trim()
        # Exit 0 without an exact group/status line is not a verified preflight.
        $Identity = "service=ltx25 group=$ExpectedGroup status="
        if ($ExitCode -eq 0 -and $StatusText.Contains($Identity)) {
            return $StatusText
        }
        $Diagnostic = if ($Failure) { $Failure } else { $StatusText }
        if ($Diagnostic.Length -gt 1000) {
            $Diagnostic = $Diagnostic.Substring(0, 1000) + " [truncated]"
        }
        Write-Warning (
            "Read-only Salad Status failed for group=$ExpectedGroup " +
            "attempt=$Attempt/3 exit_code=$ExitCode diagnostic=$Diagnostic"
        )
        if ($Attempt -lt 3) { Start-Sleep -Seconds (2 * $Attempt) }
    }
    throw "Salad Status could not confirm group=$ExpectedGroup after 3 read-only attempts; refusing GPU allocation."
}

function Prepare-LtxImage {
    param(
        [Parameter(Mandatory)][string]$Tag,
        [Parameter(Mandatory)][string]$PinnedImage,
        [switch]$IsolatedRaceGroup
    )
    if ($IsolatedRaceGroup) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Prepare -Service ltx25 -Image $Tag -PinnedImage $PinnedImage -SkipBuild -IsolatedRaceGroup -EnvFile $EnvFile -NonInteractive
    }
    else {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Prepare -Service ltx25 -Image $Tag -PinnedImage $PinnedImage -SkipBuild -EnvFile $EnvFile -NonInteractive
    }
    $ExitCode = [int]$LASTEXITCODE
    if ($ExitCode -ne 0) {
        $Target = if ($IsolatedRaceGroup) { $AuxGroupName } else { [string]$Services.services.ltx25.group_name }
        throw "Salad image prepare failed for group=$Target exit_code=$ExitCode."
    }
}

$CurrentCommit = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $CurrentCommit -ne $ExpectedCommit) {
    throw "Local Git commit differs from the reviewed experimental commit."
}
$TagSuffix = (& git rev-parse --short=12 HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Cannot derive image tag from Git commit." }
$ExpectedTag = "docker.io/yagobordell/ai-video-factory:ltx25-stage2-bootstrap-$TagSuffix"
if ($ExperimentalTag -ne $ExpectedTag) {
    throw "Experimental tag must identify the reviewed local commit: $ExpectedTag"
}
if ($ExperimentalPinnedImage -notmatch "^docker[.]io/yagobordell/ai-video-factory@sha256:[0-9a-fA-F]{64}$" -or
    $ExperimentalPinnedImage -eq $OriginalPinnedImage) {
    throw "Expected an immutable NEW experimental Docker digest."
}
if ([string]$Services.services.ltx25.environment.LTX_MODEL_DOWNLOAD_MAX_WORKERS -ne "2" -or
    [string]$Services.services.ltx25.environment.LTX_REQUIRE_VERIFIED_SHARED_MANIFEST -ne "true") {
    throw "Local Salad manifest does not enable the reviewed bootstrap protections."
}

$Audio = Join-Path $RepoRoot "data\input\avatar\monje.wav"
$Avatar = Join-Path $RepoRoot "data\input\avatar\monje.png"
$ExpectedAudioHash = "18d070d56289a7abb83abe1394bcbcc29d7cab09cf464220c38ac7297bc94553"
$ExpectedAvatarHash = "aaaefa0acd0dbf25b7526ccb349fb6ad4fd7a8511b9eadec66107244bd7bbe64"
if (-not (Test-Path -LiteralPath $Audio -PathType Leaf) -or
    -not (Test-Path -LiteralPath $Avatar -PathType Leaf)) {
    throw "Monk audio or avatar image is missing."
}
if ((Get-FileHash -LiteralPath $Audio -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedAudioHash -or
    (Get-FileHash -LiteralPath $Avatar -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedAvatarHash) {
    throw "Monk input hash differs from the previous benchmark."
}

$Inspect = @(& docker buildx imagetools inspect $ExperimentalTag)
if ($LASTEXITCODE -ne 0) { throw "Cannot inspect published experimental image." }
$Digests = @($Inspect | Select-String -Pattern "^\s*Digest:\s+(sha256:[0-9a-fA-F]{64})\s*$")
if ($Digests.Count -ne 1) { throw "Cannot resolve one immutable experimental digest." }
$Published = "docker.io/yagobordell/ai-video-factory@$($Digests[0].Matches[0].Groups[1].Value)"
if ($Published -ne $ExperimentalPinnedImage) { throw "Docker tag points to another digest." }

$InitialStatus = Get-LtxStatus
Write-Host $InitialStatus
if ($InitialStatus -notmatch "status=stopped" -or
    $InitialStatus -notmatch "replicas=1" -or
    $InitialStatus -notmatch "pending=False" -or
    -not $InitialStatus.Contains("image=$OriginalPinnedImage")) {
    throw "Salad is not safely stopped on the previous pinned image."
}

if ($StartupReplicas -eq 2) {
    $AuxInitialStatus = Get-LtxStatus -IsolatedRaceGroup
    if ($AuxInitialStatus -notmatch "status=missing" -and
        ($AuxInitialStatus -notmatch "status=stopped" -or
         $AuxInitialStatus -notmatch "replicas=1" -or
         $AuxInitialStatus -notmatch "pending=False")) {
        throw "Isolated race group is not safely stopped. Refusing to touch live capacity."
    }
}

$Prompt = "An elderly monk speaking calmly to the camera."
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$SegmentId = "ltx-a2v-monje-stage2two-parallel-race2-$Stamp"
$OutputDir = Join-Path $RepoRoot "artifacts\$SegmentId"
$PrepareAttempted = $false
$AuxPrepareAttempted = $false
$TestSucceeded = $false

try {
    $PrepareAttempted = $true
    Prepare-LtxImage -Tag $ExperimentalTag -PinnedImage $ExperimentalPinnedImage
    $PreparedStatus = Get-LtxStatus
    Write-Host $PreparedStatus
    if ($PreparedStatus -notmatch "status=stopped" -or
        $PreparedStatus -notmatch "replicas=1" -or
        $PreparedStatus -notmatch "pending=False" -or
        -not $PreparedStatus.Contains("image=$ExperimentalPinnedImage")) {
        throw "Salad did not settle with the pinned experimental image."
    }

    if ($StartupReplicas -eq 2) {
        $AuxPrepareAttempted = $true
        Prepare-LtxImage -Tag $ExperimentalTag -PinnedImage $ExperimentalPinnedImage -IsolatedRaceGroup
        $AuxStatus = Get-LtxStatus -IsolatedRaceGroup
        if ($AuxStatus -notmatch "status=stopped" -or
            $AuxStatus -notmatch "replicas=1" -or
            $AuxStatus -notmatch "pending=False" -or
            -not $AuxStatus.Contains("image=$ExperimentalPinnedImage")) {
            throw "Auxiliary isolated race group is not prepared and stopped on reviewed image."
        }
        Write-Host "LTX_ISOLATED groups=$($Services.services.ltx25.group_name),$AuxGroupName"
    }

    # The two-GPU path uses independent groups. It never scales in a ready worker.
    $BootstrapWaitSeconds = if ($StartupReplicas -eq 1) { 3600 } else { 300 }
    Write-Host "LTX_STARTUP_MODE replicas=$StartupReplicas readiness_timeout_seconds=$BootstrapWaitSeconds"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Smoke -Audio $Audio -AvatarImage $Avatar -Profile reference -ReferenceStage2Steps 2 -SegmentId $SegmentId -Prompt $Prompt -Seed 4242 -OutputDir $OutputDir -StartupReplicas $StartupReplicas -RaceTimeoutSeconds $RaceTimeoutSeconds -BootstrapTimeoutSeconds $BootstrapWaitSeconds -MaxGenerationSeconds 600 -ExpectedPinnedImage $ExperimentalPinnedImage -EnvFile $EnvFile -NonInteractive
    if ($LASTEXITCODE -ne 0) { throw "Controlled LTX two-GPU smoke failed." }

    # The smoke has already stopped GPU capacity. Review files never modify MP4.
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        $Python = (Get-Command python -ErrorAction Stop).Source
    }
    $ReviewScript = Join-Path $RepoRoot "scripts\diagnostics\ltx25_monje_review_sheet.py"
    $ReviewSheet = Join-Path $OutputDir "monje_framing_review.png"
    & $Python $ReviewScript --video (Join-Path $OutputDir "avatar_segment.mp4") --output $ReviewSheet
    if ($LASTEXITCODE -ne 0) { throw "Cannot generate visual review sheet." }
    Write-Host "VISUAL REVIEW REQUIRED: $ReviewSheet (not a passing zoom/lip-sync check)"
    $TestSucceeded = $true
}
finally {
    if ($PrepareAttempted) {
        $AuxCleanupError = $null
        if ($AuxPrepareAttempted) {
            try {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Stop -Service ltx25 -IsolatedRaceGroup -EnvFile $EnvFile -NonInteractive
                if ($LASTEXITCODE -ne 0) { throw "Cannot stop auxiliary isolated race group." }
                $AuxStopped = Get-LtxStatus -IsolatedRaceGroup
                if ($AuxStopped -notmatch "status=stopped" -or
                    $AuxStopped -notmatch "replicas=1" -or
                    $AuxStopped -notmatch "pending=False") {
                    throw "Auxiliary isolated race group did not settle stopped/1/no-pending."
                }
            }
            catch { $AuxCleanupError = $_ }
        }
        $FinalStatus = Get-LtxStatus
        Write-Host $FinalStatus

        if ($FinalStatus.Contains("image=$ExperimentalPinnedImage")) {
            if ($FinalStatus -notmatch "status=stopped" -or
                $FinalStatus -notmatch "pending=False") {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Stop -Service ltx25 -EnvFile $EnvFile -NonInteractive
                if ($LASTEXITCODE -ne 0) { throw "Salad stop failed; inspect live group immediately." }
                $FinalStatus = Get-LtxStatus
            }
            if ($FinalStatus -notmatch "status=stopped" -or
                $FinalStatus -notmatch "pending=False") {
                throw "Salad is not stably stopped; refusing to replace a live group."
            }

            if ($FinalStatus -notmatch "replicas=1") {
                throw "Unexpected main group replica configuration; refusing image restoration."
            }
            Prepare-LtxImage -Tag $OriginalTag -PinnedImage $OriginalPinnedImage
        }
        elseif (-not $FinalStatus.Contains("image=$OriginalPinnedImage")) {
            throw "Salad has an unexpected image; refusing to change a different deployment."
        }

        $RestoredStatus = Get-LtxStatus
        Write-Host $RestoredStatus
        if ($RestoredStatus -notmatch "status=stopped" -or
            $RestoredStatus -notmatch "replicas=1" -or
            $RestoredStatus -notmatch "pending=False" -or
            -not $RestoredStatus.Contains("image=$OriginalPinnedImage")) {
            throw "Salad original-image restoration could not be verified."
        }
        if ($null -ne $AuxCleanupError) { throw $AuxCleanupError }
        Write-Host "SALAD DETENIDO E IMAGEN ORIGINAL RESTAURADA" -ForegroundColor Green
    }
}
if ($TestSucceeded) {
    Write-Host "TEST EXPERIMENTAL COMPLETADO" -ForegroundColor Green
    Write-Host "Artefactos: $OutputDir"
}
