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

    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    [string]$RunId = "",

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ProductionRunner = Join-Path $PSScriptRoot "run_production.py"
$Preflight = Join-Path $PSScriptRoot "preflight_video_factory.py"
$SaladStack = Join-Path $PSScriptRoot "../salad/manage_salad_stack.ps1"
$CapacityController = Join-Path $PSScriptRoot "../salad/run_salad_capacity_controller.py"
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

    $InstallMutex = Enter-NamedMutex -Name "BordellsFlow-Remotion-Install"
    try {
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
    finally {
        Exit-NamedMutex -Mutex $InstallMutex
    }
}

function Assert-CapacityControllerHealthy {
    & python $CapacityController --check-health
    if ($LASTEXITCODE -ne 0) {
        throw "Global Salad capacity controller is not healthy."
    }
}

function Enter-NamedMutex {
    param([Parameter(Mandatory)][string]$Name)

    $Mutex = [Threading.Mutex]::new($false, $Name)
    try {
        try {
            $null = $Mutex.WaitOne()
        }
        catch [Threading.AbandonedMutexException] {
            # Ownership is transferred to this process when the previous owner died.
        }
        return $Mutex
    }
    catch {
        $Mutex.Dispose()
        throw
    }
}

function Exit-NamedMutex {
    param([Threading.Mutex]$Mutex)

    if ($null -eq $Mutex) {
        return
    }
    try {
        $Mutex.ReleaseMutex()
    }
    finally {
        $Mutex.Dispose()
    }
}
Import-EnvFile -Path $EnvFile
$ResolvedInput = Resolve-InputPath -Path $ScriptFile
if ([string]::IsNullOrWhiteSpace($RunId)) {
    $Stem = [IO.Path]::GetFileNameWithoutExtension($ResolvedInput)
    $SafeStem = ($Stem -replace '[^A-Za-z0-9._-]', '-').Trim('-')
    if ([string]::IsNullOrWhiteSpace($SafeStem)) {
        $SafeStem = "video"
    }
    $RunId = "{0}-{1}-{2}" -f $SafeStem, [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ"), [Guid]::NewGuid().ToString("N").Substring(0, 8)
}
$Document = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$Services = @($Document.stack.service_order | ForEach-Object { [string]$_ })
$OutputDir = Join-Path $RepoRoot (Join-Path "data\output\runs" $RunId)
$TempDir = Join-Path $RepoRoot (Join-Path "data\tmp\runs" $RunId)
New-Item -ItemType Directory -Path $OutputDir, $TempDir -Force | Out-Null
$env:OUTPUT_DIR = $OutputDir
$env:TEMP_DIR = $TempDir
$FinalVideo = Join-Path $OutputDir "phase9\final_video.mp4"
$ProductionMetrics = Join-Path $OutputDir "production_metrics.json"
$RunMetrics = Join-Path $OutputDir "video_factory_metrics.json"
$Overall = [Diagnostics.Stopwatch]::StartNew()
$Phase9Seconds = 0.0
$PrimaryFailure = $null

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
        Ensure-RemotionDependencies

        Write-Host "=== SALAD CAPACITY: require healthy global controller ===" -ForegroundColor Cyan
        Assert-CapacityControllerHealthy
        $env:SALAD_AUTOSCALER_ENABLED = "true"

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
            "--output-dir",
            $OutputDir,
            "--metrics",
            $ProductionMetrics
        )
        foreach ($Stage in $ForceStage) {
            $ProductionArguments += @("--force-stage", $Stage)
        }
        Invoke-Python -Arguments $ProductionArguments

        Write-Host "=== PHASE 9: composition, Remotion render, stream-copy final mux ===" `
            -ForegroundColor Cyan
        $Phase9Mutex = Enter-NamedMutex -Name "BordellsFlow-Phase9-Renderer"
        try {
            $Phase9 = [Diagnostics.Stopwatch]::StartNew()
            Invoke-Python -Arguments @($Phase9Plan)
            Invoke-Python -Arguments @($Phase9Motion)
            Invoke-Python -Arguments @($Phase9Final)
            $Phase9.Stop()
            $Phase9Seconds = $Phase9.Elapsed.TotalSeconds
        }
        finally {
            Exit-NamedMutex -Mutex $Phase9Mutex
        }

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
}

if ($null -ne $PrimaryFailure) {
    throw $PrimaryFailure
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
    run_id = $RunId
    output_dir = $OutputDir
    total_wall_clock_seconds = $Overall.Elapsed.TotalSeconds
    production_phases_2_8_seconds = $ProductionSeconds
    phase9_seconds = $Phase9Seconds
    final_video = $FinalVideo
    manual_intervention = 0
    capacity_control = "global Postgres-elected Salad controller"
    cleanup = "pipeline does not stop shared GPU services; global controller owns scale-to-zero"
}
$Metrics | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $RunMetrics -Encoding UTF8

Write-Host ""
Write-Host "VIDEO FACTORY COMPLETE" -ForegroundColor Green
Write-Host ""
Write-Host ("Run ID: {0}" -f $RunId)
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
Write-Host "Capacity cleanup: global controller owns shared scale-to-zero"
Write-Host ("Metrics: {0}" -f $RunMetrics)
