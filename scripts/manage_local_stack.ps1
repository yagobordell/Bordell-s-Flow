[CmdletBinding()]
param(
    [ValidateSet("Validate", "Build", "Smoke", "Phase9", "Production")]
    [string]$Action = "Validate",

    [string]$ComposeFile = "compose.yaml",

    [string]$ScriptFile = "data/input/script.txt",

    [string[]]$ForceStage = @(),

    [switch]$NoCache
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$VideoFactory = Join-Path $PSScriptRoot "run_video_factory.ps1"
$ComposePath = $ComposeFile
if (-not [IO.Path]::IsPathRooted($ComposePath)) {
    $ComposePath = Join-Path $RepoRoot $ComposePath
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

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker compose -f $ComposePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($Arguments -join ' ')"
    }
}

function Resolve-ContainerScriptPath {
    param([Parameter(Mandatory)][string]$Path)

    $FullPath = $Path
    if (-not [IO.Path]::IsPathRooted($FullPath)) {
        $FullPath = Join-Path $RepoRoot $FullPath
    }
    $FullPath = [IO.Path]::GetFullPath($FullPath)

    if (-not (Test-Path -LiteralPath $FullPath -PathType Leaf)) {
        throw "Production source script not found: $FullPath"
    }

    $Relative = [IO.Path]::GetRelativePath($RepoRoot, $FullPath)
    if ($Relative -eq ".." -or $Relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")) {
        throw "Production source script must live inside the repository: $FullPath"
    }

    $ContainerPath = $Relative.Replace("\", "/")
    if ($ContainerPath -ne "data" -and -not $ContainerPath.StartsWith("data/")) {
        throw "Production source script must live below data/ so it is mounted into the container."
    }
    return $ContainerPath
}

if (-not (Test-Path -LiteralPath $ComposePath -PathType Leaf)) {
    throw "Compose file not found: $ComposePath"
}

Set-Location $RepoRoot
if ($Action -ne "Production") {
    Assert-Docker
}

switch ($Action) {
    "Validate" {
        Invoke-Compose -Arguments @("config", "--quiet")
        Write-Host "Local Compose control plane: VALID" -ForegroundColor Green
    }

    "Build" {
        $Arguments = @("build")
        if ($NoCache) {
            $Arguments += "--no-cache"
        }
        $Arguments += @("orchestrator", "renderer")
        Invoke-Compose -Arguments $Arguments
        Write-Host "Local control-plane images built." -ForegroundColor Green
    }

    "Smoke" {
        Invoke-Compose -Arguments @(
            "run",
            "--rm",
            "orchestrator",
            "python",
            "scripts/local_container_smoke.py"
        )
        Invoke-Compose -Arguments @(
            "run",
            "--rm",
            "renderer",
            "python",
            "scripts/local_container_smoke.py",
            "--renderer"
        )
        Write-Host "Local control-plane smoke checks passed." -ForegroundColor Green
    }

    "Phase9" {
        Invoke-Compose -Arguments @("run", "--rm", "renderer")
        Write-Host "Local Phase 9 render completed." -ForegroundColor Green
    }

    "Production" {
        $Arguments = @{
            Input = $ScriptFile
            ForceStage = $ForceStage
            NonInteractive = $true
        }
        & $VideoFactory @Arguments
        if (-not $?) {
            throw "End-to-end Video Factory runner failed."
        }
        Write-Host "Full host-orchestrated production pipeline completed." -ForegroundColor Green
    }
}
