[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [Alias("Input")]
    [string]$ScriptFile,

    [ValidateRange(1, 16)]
    [int]$MaxParallelStages = 4,

    [ValidateRange(1, 8)]
    [int]$MaxParallelGpuStages = 2,

    [string[]]$ForceStage = @(),

    [ValidatePattern("^[A-Za-z][A-Za-z0-9_-]{1,15}$")]
    [string]$NarrationLanguage = "en",

    [string]$EnvFile = ".env",

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ProductionRunner = Join-Path $PSScriptRoot "run_production.py"
$Preflight = Join-Path $PSScriptRoot "preflight_video_factory.py"
$SaladStack = Join-Path $PSScriptRoot "manage_salad_stack.ps1"
$ZeroReplicaGuard = Join-Path $PSScriptRoot "ensure_salad_zero_replicas.ps1"
$QueueCleanup = Join-Path $PSScriptRoot "cleanup_salad_queue.ps1"
$Phase9Plan = Join-Path $PSScriptRoot "run_phase9_compositor.py"
$Phase9Motion = Join-Path $PSScriptRoot "run_phase9_motion.py"
$Phase9Final = Join-Path $PSScriptRoot "run_phase9_final.py"
$ServicesPath = Join-Path $RepoRoot "deploy\salad\services.json"

function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path $RepoRoot $Resolved
    }
    if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
        throw "Video Factory environment file not found: $Resolved"
    }

    foreach ($RawLine in Get-Content -LiteralPath $Resolved) {
        $Line = $RawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($Line) -or $Line.StartsWith("#")) {
            continue
        }
        if ($Line -notmatch '^(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$') {
            continue
        }
        $Name = $Matches["name"]
        $Value = $Matches["value"].Trim()
        if ($Value.Length -ge 2) {
            $First = $Value.Substring(0, 1)
            $Last = $Value.Substring($Value.Length - 1, 1)
            if (($First -eq '"' -and $Last -eq '"') -or ($First -eq "'" -and $Last -eq "'")) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }
        }
        [Environment]::SetEnvironmentVariable(
            $Name,
            $Value,
            [EnvironmentVariableTarget]::Process
        )
    }
}

function Resolve-InputPath {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path $RepoRoot $Resolved
    }
    $Resolved = [IO.Path]::GetFullPath($Resolved)
    if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
        throw "Video Factory input not found: $Resolved"
    }
    return $Resolved
}

function Invoke-Python {
    param([Parameter(Mandatory)][object[]]$Arguments)

    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code ${LASTEXITCODE}: python $($Arguments -join ' ')"
    }
}

function Ensure-RemotionDependencies {
    $Binary = Join-Path $RepoRoot "remotion\node_modules\.bin\remotion.cmd"
    if (Test-Path -LiteralPath $Binary -PathType Leaf) {
        return
    }

    $Npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $Npm) {
        $Npm = Get-Command npm -ErrorAction SilentlyContinue
    }
    if ($null -eq $Npm) {
        throw "npm is required to install the isolated Remotion renderer."
    }

    Write-Host "=== Renderer dependencies: npm ci (one-time/local cacheable) ===" `
        -ForegroundColor Cyan
    Push-Location (Join-Path $RepoRoot "remotion")
    try {
        $NpmExecutable = [string]$Npm.Source
        & $NpmExecutable ci
        if ($LASTEXITCODE -ne 0) {
            throw "npm ci failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

function Invoke-FinalCleanup {
    param([Parameter(Mandatory)][string[]]$Services)

    $Failures = @()
    try {
        & $SaladStack -Action Stop -Services $Services -EnvFile $EnvFile -NonInteractive
        if (-not $?) {
            throw "Salad stack stop returned failure."
        }
    }
    catch {
        $Failures += $_
        Write-Warning "Global Salad stop failed: $($_.Exception.Message)"
    }

    foreach ($Service in $Services) {
        try {
            & $ZeroReplicaGuard `
                -Service $Service `
                -EnvFile $EnvFile `
                -NonInteractive
            if (-not $?) {
                throw "Zero-replica recheck failed for $Service."
            }

            & $QueueCleanup `
                -Service $Service `
                -EnvFile $EnvFile `
                -TimeoutSeconds 180 `
                -NonInteractive
            if (-not $?) {
                throw "Queue cleanup failed for $Service."
            }
        }
        catch {
            $Failures += $_
            Write-Warning "Final queue cleanup failed for ${Service}: $($_.Exception.Message)"
        }
    }
    if ($Failures.Count -gt 0) {
        throw $Failures[0]
    }
}

Import-EnvFile -Path $EnvFile
$ResolvedInput = Resolve-InputPath -Path $ScriptFile
$Document = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$Services = @($Document.stack.service_order | ForEach-Object { [string]$_ })
$OutputDir = Join-Path $RepoRoot "data\output"
$FinalVideo = Join-Path $OutputDir "phase9\final_video.mp4"
$ProductionMetrics = Join-Path $OutputDir "production_metrics.json"
$RunMetrics = Join-Path $OutputDir "video_factory_metrics.json"
$SaladPreflightPassed = $false
$Overall = [Diagnostics.Stopwatch]::StartNew()
$Phase9Seconds = 0.0
$PrimaryFailure = $null
$CleanupFailure = $null

try {
    Write-Host "=== VIDEO FACTORY PREFLIGHT: no GPU allocation ===" -ForegroundColor Cyan
    Push-Location $RepoRoot
    try {
        Invoke-Python -Arguments @(
            $Preflight,
            $ResolvedInput,
            "--report",
            (Join-Path $OutputDir "preflight_report.json")
        )
        & $SaladStack -Action Status -Services $Services -EnvFile $EnvFile -NonInteractive
        if (-not $?) {
            throw "Salad control-plane preflight failed."
        }
        $SaladPreflightPassed = $true
        # The preflight has already enumerated every Salad queue and confirmed that
        # no pending/running job exists. Let the controlled GPU runners reuse that
        # same proof during this process; otherwise a stale queue counter forces a
        # second slow historical pagination pass before every prewarm.
        $env:AI_VIDEO_FACTORY_PREFLIGHT_QUEUE_EMPTY = "1"
        $env:AI_VIDEO_FACTORY_PREFLIGHT_REPORT = Join-Path $OutputDir "preflight_report.json"

        Ensure-RemotionDependencies

        Write-Host "=== VIDEO FACTORY DAG: cache/resume + bounded parallel execution ===" `
            -ForegroundColor Cyan
        $ProductionArguments = @(
            $ProductionRunner,
            $ResolvedInput,
            "--end-to-end",
            "--max-parallel-stages",
            $MaxParallelStages,
            "--max-parallel-gpu-stages",
            $MaxParallelGpuStages,
            "--narration-language",
            $NarrationLanguage,
            "--metrics",
            $ProductionMetrics
        )
        foreach ($Stage in $ForceStage) {
            $ProductionArguments += @("--force-stage", $Stage)
        }
        Invoke-Python -Arguments $ProductionArguments

        Write-Host "=== PHASE 9: composition, Remotion render, stream-copy final mux ===" `
            -ForegroundColor Cyan
        $Phase9 = [Diagnostics.Stopwatch]::StartNew()
        Invoke-Python -Arguments @($Phase9Plan)
        Invoke-Python -Arguments @($Phase9Motion)
        Invoke-Python -Arguments @($Phase9Final)
        $Phase9.Stop()
        $Phase9Seconds = $Phase9.Elapsed.TotalSeconds

        if (-not (Test-Path -LiteralPath $FinalVideo -PathType Leaf)) {
            throw "FinalVideo was not created: $FinalVideo"
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    $PrimaryFailure = $_
}
finally {
    $Overall.Stop()
    if ($SaladPreflightPassed) {
        Write-Host "=== FINAL CLEANUP: stop workers, replicas=0, clean queues ===" `
            -ForegroundColor Cyan
        try {
            Invoke-FinalCleanup -Services $Services
        }
        catch {
            $CleanupFailure = $_
        }
    }
}

if ($null -ne $PrimaryFailure) {
    if ($null -ne $CleanupFailure) {
        Write-Warning (
            "Final cleanup also failed; preserving the original pipeline error. " +
            "Cleanup error: $($CleanupFailure.Exception.Message)"
        )
    }
    throw $PrimaryFailure
}
if ($null -ne $CleanupFailure) {
    throw $CleanupFailure
}

$ProductionDocument = $null
if (Test-Path -LiteralPath $ProductionMetrics -PathType Leaf) {
    $ProductionDocument = Get-Content -LiteralPath $ProductionMetrics -Raw | ConvertFrom-Json
}
$ProductionSeconds = $null
if ($null -ne $ProductionDocument) {
    $ProductionSeconds = [double]$ProductionDocument.total_elapsed_seconds
}
$Metrics = [ordered]@{
    schema_version = "1"
    total_wall_clock_seconds = $Overall.Elapsed.TotalSeconds
    production_phases_2_8_seconds = $ProductionSeconds
    phase9_seconds = $Phase9Seconds
    final_video = $FinalVideo
    manual_intervention = 0
    cleanup = "all project GPU services stopped; replicas=0 guard applied; queues cleaned"
}
$Metrics | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $RunMetrics -Encoding UTF8

Write-Host ""
Write-Host "VIDEO FACTORY COMPLETE" -ForegroundColor Green
Write-Host ""
Write-Host "Final video:"
Write-Host $FinalVideo
Write-Host ""
Write-Host ("Total wall-clock: {0:N1} s" -f $Overall.Elapsed.TotalSeconds)
if ($null -ne $ProductionDocument) {
    $CacheHits = @(
        $ProductionDocument.stages |
            Where-Object { $_.outcome -eq "skipped" -or $_.outcome -eq "adopted" }
    ).Count
    Write-Host ("Cache/resume stage hits: {0}" -f $CacheHits)
}
Write-Host "Manual intervention: 0"
Write-Host "Final cleanup: all project GPU services stopped; replicas=0; queues cleaned"
Write-Host ("Metrics: {0}" -f $RunMetrics)
