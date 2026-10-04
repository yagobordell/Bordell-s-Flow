[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Audio,
    [Parameter(Mandatory)][string]$AvatarImage,
    [Parameter(Mandatory)][string]$Prompt,
    [Parameter(Mandatory)][string]$ExperimentalImage,
    [Parameter(Mandatory)][string]$OriginalProductionImage,
    [ValidateSet(
        "reference-bench-cpu", "reference-fp8none",
        "reference-cpu-triton", "reference-none-triton",
        "static-bench-cpu", "static-fp8none"
    )][string[]]$Profiles = @("reference-bench-cpu", "reference-fp8none"),
    [ValidateRange(3, 10)][int]$Repetitions = 3,
    [long]$Seed = 4242,
    [ValidateRange(0, 21600)][int]$MaxProfileTotalSeconds = 0,
    [switch]$ContinueOnTerminalFailure,
    [string]$OutputDir = "",
    [string]$EnvFile = "config/salad.env",
    [ValidateRange(120, 21600)][int]$BootstrapTimeoutSeconds = 7200
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($env:A2V_REUSE_WRAPPER_ACTIVE) {
    throw "Nested production reuse is not supported"
}
foreach ($Value in @($ExperimentalImage, $OriginalProductionImage)) {
    if ($Value -notmatch '^[^\s]+@sha256:[0-9a-fA-F]{64}$') {
        throw "Immutable original and experimental image digests are mandatory"
    }
}
if ($OriginalProductionImage -eq $ExperimentalImage) {
    throw "Original and experimental images cannot match"
}
foreach ($Path in @($Audio, $AvatarImage)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing benchmark input: $Path"
    }
}
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
# All mutating Salad operations require a session-capable Postgres advisory
# lock. Resolve its DSN BEFORE attempting Prepare, not after allocating work.
$RepoRoot = $Root
. (Join-Path $Root 'scripts\salad\_env_file.ps1')
Import-EnvFile -Path $EnvFile
if ([string]::IsNullOrWhiteSpace($env:SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN) -and
    [string]::IsNullOrWhiteSpace($env:POSTGRES_DSN)) {
    throw "Missing Postgres DSN for Capacity Controller lock. Set a direct or session-mode DSN in the active checkout or the selected EnvFile's companion app.env."
}
$Manager = Join-Path $Root 'scripts\salad\manage_salad_worker.ps1'
$Isolation = Join-Path $PSScriptRoot 'check_ltx_a2v_benchmark_isolation.py'
$QueueGate = Join-Path $PSScriptRoot 'check_ltx_a2v_benchmark_jobs.py'
$Preflight = Join-Path $Root 'scripts\pipeline\check_r2_ready.py'
$Runner = Join-Path $PSScriptRoot 'run_ltx25_a2v_benchmark_batch.ps1'
$Restore = Join-Path $PSScriptRoot 'restore_ltx25_a2v_production.ps1'
& $Python -c 'from ai_video_factory.inference.capacity_controller import resolve_capacity_controller_dsn; resolve_capacity_controller_dsn(); print("CAPACITY_CONTROLLER_DSN_OK")'
if ($LASTEXITCODE -ne 0) {
    throw "Capacity Controller DSN is invalid or transaction-pooled; use direct/session Postgres DSN."
}
$OutputDir = if ($OutputDir) {
    [IO.Path]::GetFullPath($OutputDir)
} else {
    Join-Path $Root ("data\output\deployment-validation\ltx25-a2v-production-reuse-" + (Get-Date -Format 'yyyyMMddHHmmss'))
}
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$Recovery = Join-Path $OutputDir 'recovery.txt'
@"
If this PowerShell process is interrupted and restoration has NOT been verified,
keep the production group STOPPED and run this recovery command:

& '$Restore' -OriginalProductionImage '$OriginalProductionImage' -ExperimentalImage '$ExperimentalImage' -EnvFile '$EnvFile'

Do not restart production until RESTORE_OK is printed.
"@ | Set-Content -LiteralPath $Recovery -Encoding UTF8
Write-Host "Recovery instructions saved to $Recovery"

# All checks happen BEFORE changing the production group.
& $Python $Preflight
if ($LASTEXITCODE -ne 0) { throw "R2 preflight failed; production unchanged" }
& $Python $Isolation --env-file $EnvFile --reuse-production `
    --expected-production-image $OriginalProductionImage --expected-task-mode a2v
if ($LASTEXITCODE -ne 0) { throw "Production group must be stopped with its original image/mode" }
& $Python $QueueGate --include-production
if ($LASTEXITCODE -ne 0) { throw "A2V production/experiment queue not empty; production unchanged" }

# No override of the global controller lock: if a controller takes
# ownership, every manual mutation fails closed.
$PrepareAttempted = $false
$SavedMarker = $env:A2V_REUSE_WRAPPER_ACTIVE
try {
    $PrepareAttempted = $true
    & $Manager -Action Prepare -Service ltx25 -EnvFile $EnvFile -NonInteractive `
        -SkipBuild -PinnedImage $ExperimentalImage `
        -ExpectedCurrentImage $OriginalProductionImage `
        -ExpectedCurrentTaskMode a2v -TaskModeOverride a2v_experimental `
        -PreserveExistingEnvironment
    if (-not $?) { throw "Could not configure experimental task mode on production group" }

    & $Python $Isolation --env-file $EnvFile --reuse-production `
        --expected-production-image $ExperimentalImage --expected-task-mode a2v_experimental
    if ($LASTEXITCODE -ne 0) { throw "Post-prepare isolation check failed" }

    $env:A2V_REUSE_WRAPPER_ACTIVE = "1"
    $RunArgs = @{
        Audio = $Audio
        AvatarImage = $AvatarImage
        Prompt = $Prompt
        ExpectedPinnedImage = $ExperimentalImage
        Profiles = $Profiles
        Repetitions = $Repetitions
        Seed = $Seed
        MaxProfileTotalSeconds = $MaxProfileTotalSeconds
        ContinueOnTerminalFailure = $ContinueOnTerminalFailure
        BootstrapTimeoutSeconds = $BootstrapTimeoutSeconds
        OutputDir = $OutputDir
        EnvFile = $EnvFile
        NonInteractive = $true
        ReuseProductionGroup = $true
    }
    & $Runner @RunArgs
    if (-not $?) { throw "Benchmark failed; initiating production restoration" }
}
finally {
    $env:A2V_REUSE_WRAPPER_ACTIVE = $SavedMarker
    if ($PrepareAttempted) {
        # Restore runs even after failed bootstrap, timeout or a failed sample.
        & $Restore -OriginalProductionImage $OriginalProductionImage `
            -ExperimentalImage $ExperimentalImage -EnvFile $EnvFile
        if (-not $?) {
            throw "CRITICAL: production restoration was not verified; keep group stopped and use $Recovery"
        }
    }
}
Write-Host "BENCHMARK_FINISHED_PRODUCTION_RESTORED manifest=$(Join-Path $OutputDir 'manifest.json')" -ForegroundColor Green
