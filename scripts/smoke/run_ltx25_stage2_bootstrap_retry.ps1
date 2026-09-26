[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExperimentalTag,
    [Parameter(Mandatory)][string]$ExperimentalPinnedImage,
    [ValidateRange(120, 7200)][int]$RaceTimeoutSeconds = 5400,
    [string]$EnvFile = ".env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
Set-Location $RepoRoot

$Manager = Join-Path $RepoRoot "scripts\salad\manage_salad_worker.ps1"
$Smoke = Join-Path $PSScriptRoot "run_ltx25_a2v_smoke_controlled.ps1"
$RaceSelector = Join-Path $PSScriptRoot "start_ltx25_race_select.py"
$Services = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\salad\services.json") -Raw | ConvertFrom-Json
$OriginalTag = [string]$Services.services.ltx25.image
$OriginalPinnedImage = "docker.io/yagobordell/ai-video-factory@sha256:3a46d675c4ca93ea065c0540ba38efdb7a45cf447cecb841524aaee0d7342469"

function Get-LtxStatus {
    $Lines = @(
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive
    )
    if ($LASTEXITCODE -ne 0) { throw "Cannot query Salad status." }
    return ($Lines -join " ")
}

function Prepare-LtxImage {
    param([Parameter(Mandatory)][string]$Tag, [Parameter(Mandatory)][string]$PinnedImage)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Prepare -Service ltx25 -Image $Tag -PinnedImage $PinnedImage -SkipBuild -EnvFile $EnvFile -NonInteractive
    if ($LASTEXITCODE -ne 0) { throw "Salad image prepare failed." }
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

$Prompt = "A locked-off shot with exactly the same camera distance, framing, face size, shoulder position and background as the reference image throughout the entire video. No zoom or reframing. The person speaks directly to camera with clear visible lip and jaw articulation precisely synchronized to the supplied speech audio. Lip sync matches every spoken phoneme. Natural blinking and restrained head movement. Only the facial movements needed for speech; the composition remains unchanged."
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$SegmentId = "ltx-a2v-monje-stage2two-parallel-race2-$Stamp"
$OutputDir = Join-Path $RepoRoot "artifacts\$SegmentId"
$PrepareAttempted = $false
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

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Smoke -Audio $Audio -AvatarImage $Avatar -Profile reference -ReferenceStage2Steps 2 -SegmentId $SegmentId -Prompt $Prompt -Seed 4242 -OutputDir $OutputDir -StartupReplicas 2 -RaceTimeoutSeconds $RaceTimeoutSeconds -BootstrapTimeoutSeconds 300 -MaxGenerationSeconds 600 -ExpectedPinnedImage $ExperimentalPinnedImage -EnvFile $EnvFile -NonInteractive
    if ($LASTEXITCODE -ne 0) { throw "Controlled LTX two-GPU smoke failed." }
    $TestSucceeded = $true
}
finally {
    if ($PrepareAttempted) {
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

            if ($FinalStatus -match "replicas=2\b") {
                $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
                if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
                    $Python = (Get-Command python -ErrorAction Stop).Source
                }
                & $Python $RaceSelector --reset-stopped --env-file $EnvFile --expected-image $ExperimentalPinnedImage
                if ($LASTEXITCODE -ne 0) { throw "Cannot restore stopped group to one replica." }
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
        Write-Host "SALAD DETENIDO E IMAGEN ORIGINAL RESTAURADA" -ForegroundColor Green
    }
}
if ($TestSucceeded) {
    Write-Host "TEST EXPERIMENTAL COMPLETADO" -ForegroundColor Green
    Write-Host "Artefactos: $OutputDir"
}
