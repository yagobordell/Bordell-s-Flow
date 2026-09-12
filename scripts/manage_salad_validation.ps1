[CmdletBinding()]
param(
    [ValidateSet("Validate", "Prepare", "Start", "Status", "Smoke", "ProtectedSmoke", "Stop")]
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
$ScaleToZeroStarter = Join-Path $PSScriptRoot "start_salad_scale_to_zero.ps1"
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$ComposePath = $ComposeFile
if (-not [IO.Path]::IsPathRooted($ComposePath)) {
    $ComposePath = Join-Path $RepoRoot $ComposePath
}

function Get-SelectedServiceNames {
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Salad stack manifest not found: $ManifestPath"
    }

    $Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($Service -eq "all") {
        return @($Document.stack.service_order | ForEach-Object { [string]$_ })
    }
    return @($Service)
}

function Use-ManifestEnvironment {
    param([Parameter(Mandatory)][scriptblock]$ScriptBlock)

    $Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    $Previous = @{}
    foreach ($Name in Get-SelectedServiceNames) {
        $Definition = $Document.services.PSObject.Properties[$Name].Value
        foreach ($Property in $Definition.environment.PSObject.Properties) {
            $EnvironmentName = [string]$Property.Name
            if (-not $Previous.ContainsKey($EnvironmentName)) {
                $Previous[$EnvironmentName] = [Environment]::GetEnvironmentVariable(
                    $EnvironmentName,
                    [EnvironmentVariableTarget]::Process
                )
            }
            [Environment]::SetEnvironmentVariable(
                $EnvironmentName,
                [string]$Property.Value,
                [EnvironmentVariableTarget]::Process
            )
        }
    }

    try {
        & $ScriptBlock
    }
    finally {
        foreach ($Entry in $Previous.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable(
                [string]$Entry.Key,
                $Entry.Value,
                [EnvironmentVariableTarget]::Process
            )
        }
    }
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

    $InvokeStack = {
        & $StackManager @Arguments
        $CallSucceeded = $?
        if (-not $CallSucceeded) {
            throw "Salad stack action failed: $StackAction"
        }
    }

    if ($StackAction -eq "Prepare") {
        Use-ManifestEnvironment -ScriptBlock $InvokeStack
        return
    }
    & $InvokeStack
}

function Invoke-ScaleToZeroStart {
    param([switch]$AllowBootstrapReplica)

    if (-not (Test-Path -LiteralPath $ScaleToZeroStarter -PathType Leaf)) {
        throw "Scale-to-zero starter not found: $ScaleToZeroStarter"
    }
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Salad stack manifest not found: $ManifestPath"
    }

    $Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    $Services = if ($Service -eq "all") {
        @($Document.stack.service_order | ForEach-Object { [string]$_ })
    }
    else {
        @($Service)
    }

    foreach ($Name in $Services) {
        Write-Host "=== Salad Start : $Name ===" -ForegroundColor Cyan
        $Arguments = @{
            Service = $Name
            EnvFile = $EnvFile
        }
        if ($AllowBootstrapReplica) {
            $Arguments["AllowBootstrapReplica"] = $true
        }
        if ($NonInteractive) {
            $Arguments["NonInteractive"] = $true
        }
        & $ScaleToZeroStarter @Arguments
        $CallSucceeded = $?
        if (-not $CallSucceeded) {
            throw "Salad scale-to-zero Start failed for service '$Name'."
        }
    }

    Write-Host (
        "Salad scale-to-zero Start complete: services={0}" -f ($Services -join ",")
    ) -ForegroundColor Green
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

function Invoke-ProtectedSmoke {
    if ($Service -eq "all") {
        throw "ProtectedSmoke requires one explicit service so GPU workers stay serialized."
    }

    try {
        Invoke-ScaleToZeroStart -AllowBootstrapReplica
        Invoke-Smoke
    }
    finally {
        Invoke-StackAction -StackAction "Stop"
    }
}

switch ($Action) {
    "Validate" { Invoke-StackAction -StackAction "Validate" }
    "Prepare" { Invoke-StackAction -StackAction "Prepare" }
    "Start" { Invoke-ScaleToZeroStart }
    "Status" { Invoke-StackAction -StackAction "Status" }
    "Smoke" { Invoke-Smoke }
    "ProtectedSmoke" { Invoke-ProtectedSmoke }
    "Stop" { Invoke-StackAction -StackAction "Stop" }
}
