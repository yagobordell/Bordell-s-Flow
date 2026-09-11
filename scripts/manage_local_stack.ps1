[CmdletBinding()]
param(
    [ValidateSet("Validate", "Build", "Smoke", "Phase9")]
    [string]$Action = "Validate",

    [string]$ComposeFile = "compose.yaml",

    [switch]$NoCache
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
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

if (-not (Test-Path -LiteralPath $ComposePath -PathType Leaf)) {
    throw "Compose file not found: $ComposePath"
}

Set-Location $RepoRoot
Assert-Docker

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
}
