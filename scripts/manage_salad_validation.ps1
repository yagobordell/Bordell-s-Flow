[CmdletBinding()]
param(
    [ValidateSet("Validate", "Prepare", "Start", "Status", "Smoke", "Stop")]
    [string]$Action = "Status",

    [ValidateSet("whisper", "breeze_tts2", "ideogram4", "ltx25", "all")]
    [string]$Service = "all",

    [string]$EnvFile = ".env",

    [string]$ComposeFile = "compose.yaml",

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [switch]$SkipBuild,

    [switch]$SkipLocalBuild,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$StackManager = Join-Path $PSScriptRoot "manage_salad_stack.ps1"
$ComposePath = $ComposeFile
if (-not [IO.Path]::IsPathRooted($ComposePath)) {
    $ComposePath = Join-Path $RepoRoot $ComposePath
}

function Invoke-StackAction {
    param([Parameter(Mandatory)][string]$StackAction)

    $Arguments = @{
        Action = $StackAction
        EnvFile = $EnvFile
        PrepareTimeoutMinutes = $PrepareTimeoutMinutes
    }
    if ($Service -ne "all") {
        $Arguments["Services"] = @($Service)
    }
    if ($SkipBuild) {
        $Arguments["SkipBuild"] = $true
    }
    if ($NonInteractive) {
        $Arguments["NonInteractive"] = $true
    }

    & $StackManager @Arguments
    $CallSucceeded = $?
    if (-not $CallSucceeded) {
        throw "Salad stack action failed: $StackAction"
    }
}

function Assert-Docker {
    & docker version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker is not available or Docker Desktop is not running."
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose v2 is not available."
    }
}

function Invoke-Smoke {
    Assert-Docker
    if (-not (Test-Path -LiteralPath $ComposePath -PathType Leaf)) {
        throw "Compose file not found: $ComposePath"
    }

    Set-Location $RepoRoot
    if (-not $SkipLocalBuild) {
        & docker compose -f $ComposePath build orchestrator
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to build the local orchestrator image."
        }
    }

    & docker compose -f $ComposePath run --rm orchestrator `
        python scripts/run_salad_smoke_suite.py `
        --service $Service `
        --output-dir /workspace/data/output/deployment-validation
    if ($LASTEXITCODE -ne 0) {
        throw "Real Salad smoke failed for service: $Service"
    }

    Write-Host "Real Salad smoke passed for: $Service" -ForegroundColor Green
}

switch ($Action) {
    "Validate" { Invoke-StackAction -StackAction "Validate" }
    "Prepare" { Invoke-StackAction -StackAction "Prepare" }
    "Start" { Invoke-StackAction -StackAction "Start" }
    "Status" { Invoke-StackAction -StackAction "Status" }
    "Smoke" { Invoke-Smoke }
    "Stop" { Invoke-StackAction -StackAction "Stop" }
}
