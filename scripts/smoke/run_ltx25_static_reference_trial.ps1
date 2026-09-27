[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ExpectedCommit,
    [Parameter(Mandatory)][string]$ExperimentalTag,
    [Parameter(Mandatory)][string]$ExperimentalPinnedImage,
    [string]$EnvFile = ".env",
    [ValidateRange(120, 21600)][int]$BootstrapTimeoutSeconds = 3600,
    [ValidateRange(120, 3600)][int]$MaxGenerationSeconds = 900
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Manager = Join-Path $RepoRoot "scripts\salad\manage_salad_worker.ps1"
$ControlledSmoke = Join-Path $PSScriptRoot "run_ltx25_a2v_smoke_controlled.ps1"
$ServiceManifest = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy\salad\services.json") -Raw | ConvertFrom-Json
$OriginalTag = [string]$ServiceManifest.services.ltx25.image
$OriginalPinnedImage = "docker.io/yagobordell/ai-video-factory@sha256:3a46d675c4ca93ea065c0540ba38efdb7a45cf447cecb841524aaee0d7342469"

if ($ExpectedCommit -notmatch '^[0-9a-fA-F]{40}$' -or
    $ExperimentalPinnedImage -notmatch '^docker\.io/yagobordell/ai-video-factory@sha256:[0-9a-fA-F]{64}$') {
    throw "Static reference requires exact reviewed 40-character SHA and immutable Docker digest."
}
$ShortCommit = $ExpectedCommit.Substring(0, 12)
if ($ExperimentalTag -ne "docker.io/yagobordell/ai-video-factory:ltx25-static-reference-$ShortCommit") {
    throw "ExperimentalTag must identify this reviewed Git commit; refusing an unrelated worker."
}
$CurrentBranch = (& git -C $RepoRoot branch --show-current).Trim()
$CurrentCommit = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or
    $CurrentBranch -ne "investigate/ltx25-monje-lipsync-latency-20260926" -or
    $CurrentCommit -ne $ExpectedCommit) {
    throw "The current worktree does not match the reviewed PR #235 commit."
}
if (@(& git -C $RepoRoot status --porcelain --untracked-files=no).Count -gt 0) {
    throw "Tracked local changes are present; refusing a paid GPU test."
}

$Avatar = Join-Path $RepoRoot "data\input\avatar\monje.png"
$Audio = Join-Path $RepoRoot "data\input\avatar\monje.wav"
$ExpectedInputs = @{
    $Avatar = "aaaefa0acd0dbf25b7526ccb349fb6ad4fd7a8511b9eadec66107244bd7bbe64"
    $Audio = "18d070d56289a7abb83abe1394bcbcc29d7cab09cf464220c38ac7297bc94553"
}
foreach ($InputPath in $ExpectedInputs.Keys) {
    if (-not (Test-Path -LiteralPath $InputPath -PathType Leaf)) {
        throw "Missing original monk input: $InputPath"
    }
    $ActualHash = (Get-FileHash -LiteralPath $InputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualHash -ne $ExpectedInputs[$InputPath]) {
        throw "Original monk input hash mismatch: $InputPath"
    }
}

function Get-MonkGroupStatus {
    $Lines = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Status -Service ltx25 -EnvFile $EnvFile -NonInteractive)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not verify the LTX Salad group status; refusing allocation."
    }
    return ($Lines -join " ")
}

function Assert-MonkGroupStopped {
    param([Parameter(Mandatory)][string]$PinnedImage)
    $Status = Get-MonkGroupStatus
    Write-Host $Status
    if ($Status -notmatch "status=stopped" -or
        $Status -notmatch "replicas=1" -or
        $Status -notmatch "pending=False" -or
        -not $Status.Contains("image=$PinnedImage")) {
        throw "LTX group must be stopped, settled, one replica, and pinned to the expected image."
    }
}

# No paid operation until all local inputs and the original remote state pass.
Assert-MonkGroupStopped -PinnedImage $OriginalPinnedImage
$SegmentId = "monje-static-reference-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$OutputDir = Join-Path $RepoRoot "artifacts\$SegmentId"
$PrepareAttempted = $false
try {
    $PrepareAttempted = $true
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Prepare -Service ltx25 -Image $ExperimentalTag -PinnedImage $ExperimentalPinnedImage -SkipBuild -EnvFile $EnvFile -NonInteractive
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the reviewed static-reference image." }
    Assert-MonkGroupStopped -PinnedImage $ExperimentalPinnedImage

    # User positive text is passed verbatim; the opt-in profile provides the
    # negative-camera conditioning. This smoke uses ONE GPU, not a capacity race.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ControlledSmoke -Audio $Audio -AvatarImage $Avatar -Profile static-reference -SegmentId $SegmentId -Prompt "An elderly monk speaking calmly to the camera." -Seed 4242 -OutputDir $OutputDir -ExpectedPinnedImage $ExperimentalPinnedImage -StartupReplicas 1 -BootstrapTimeoutSeconds $BootstrapTimeoutSeconds -MaxGenerationSeconds $MaxGenerationSeconds -EnvFile $EnvFile -NonInteractive
    if ($LASTEXITCODE -ne 0) { throw "Static-reference smoke failed; see its lifecycle metrics." }
    Write-Host "STATIC_REFERENCE_OUTPUT=$OutputDir"
}
finally {
    if ($PrepareAttempted) {
        # The controlled smoke already stops in finally; this second Stop is
        # intentional for exceptions during prepare, startup and validation.
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Stop -Service ltx25 -EnvFile $EnvFile -NonInteractive
        if ($LASTEXITCODE -ne 0) {
            throw "Cannot confirm STOP. Do not restore the image or start another test."
        }
        Assert-MonkGroupStopped -PinnedImage $ExperimentalPinnedImage
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Manager -Action Prepare -Service ltx25 -Image $OriginalTag -PinnedImage $OriginalPinnedImage -SkipBuild -EnvFile $EnvFile -NonInteractive
        if ($LASTEXITCODE -ne 0) { throw "Could not restore the original pinned image." }
        Assert-MonkGroupStopped -PinnedImage $OriginalPinnedImage
        Write-Host "SALAD DETENIDO E IMAGEN ORIGINAL RESTAURADA" -ForegroundColor Green
    }
}
