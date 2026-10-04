[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OriginalProductionImage,
    [Parameter(Mandatory)][string]$ExperimentalImage,
    [string]$EnvFile = 'config/salad.env'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

foreach ($Value in @($OriginalProductionImage, $ExperimentalImage)) {
    if ($Value -notmatch '^[^\s]+@sha256:[0-9a-fA-F]{64}$') {
        throw "Recovery requires exact immutable image digests"
    }
}
if ($OriginalProductionImage -eq $ExperimentalImage) {
    throw "Recovery image digests must differ"
}

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Manager = Join-Path $Root 'scripts\salad\manage_salad_worker.ps1'
$Isolation = Join-Path $PSScriptRoot 'check_ltx_a2v_benchmark_isolation.py'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

# Never restore on top of an unexpected third-party image.
$StatusText = (& $Manager -Action Status -Service ltx25 -EnvFile $EnvFile | Out-String).Trim()
if ($StatusText -match 'status=missing') { throw "Production group is missing; operator recovery required" }
if ($StatusText -match 'image=([^\s]+)') {
    $Current = $Matches[1]
} else {
    throw "Cannot read current production image"
}
if ($Current -notin @($OriginalProductionImage, $ExperimentalImage)) {
    throw "Refusing automatic recovery: group image was changed by another operator: $Current"
}

if ($Current -eq $ExperimentalImage) {
    # Stop must settle before any image or environment mutation.
    & $Manager -Action Stop -Service ltx25 -EnvFile $EnvFile -NonInteractive
    if (-not $?) { throw "Could not stop experimental worker; production has NOT been restored" }

    & $Python $Isolation --env-file $EnvFile --reuse-production `
        --expected-production-image $ExperimentalImage --expected-task-mode a2v_experimental
    if ($LASTEXITCODE -ne 0) {
        throw "Recovery guard failed: do not overwrite an unexpected task mode or running group"
    }
    & $Manager -Action Prepare -Service ltx25 -EnvFile $EnvFile -NonInteractive `
        -SkipBuild -PinnedImage $OriginalProductionImage `
        -ExpectedCurrentImage $ExperimentalImage `
        -ExpectedCurrentTaskMode a2v_experimental -TaskModeOverride a2v `
        -PreserveExistingEnvironment
    if (-not $?) { throw "Production image/mode restoration failed; keep the group stopped" }
}

& $Python $Isolation --env-file $EnvFile --reuse-production `
    --expected-production-image $OriginalProductionImage --expected-task-mode a2v
if ($LASTEXITCODE -ne 0) {
    throw "Restoration verification failed: do not restart production"
}
Write-Host 'RESTORE_OK: production image and a2v task mode restored; group is stopped' -ForegroundColor Green
