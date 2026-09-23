[CmdletBinding()]
param(
    [ValidateSet("Validate", "Prepare", "Start", "Prewarm", "Status", "Smoke", "ProtectedSmoke", "Stop")]
    [string]$Action = "Status",

    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "qwen_image_21", "ltx25", "realesrgan", "all")]
    [string]$Service = "all",

    [string]$EnvFile = ".env",

    [string]$ComposeFile = "compose.yaml",

    [string]$PinnedImage = "",

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [ValidateRange(10, 180)]
    [int]$PrewarmTimeoutMinutes = 90,

    [switch]$SkipBuild,

    [switch]$Recreate,

    [switch]$SkipLocalBuild,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$StackManager = Join-Path $PSScriptRoot "manage_salad_stack.ps1"
$ScaleToZeroStarter = Join-Path $PSScriptRoot "start_salad_scale_to_zero.ps1"
$ProtectedSmokeBootstrap = Join-Path $PSScriptRoot "start_salad_protected_smoke.ps1"
$ScaleToZeroRestorer = Join-Path $PSScriptRoot "restore_salad_scale_to_zero.ps1"
$ZeroReplicaGuard = Join-Path $PSScriptRoot "ensure_salad_zero_replicas.ps1"
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
        foreach ($Property in @($Document.stack.shared_environment.PSObject.Properties) + @($Definition.environment.PSObject.Properties)) {
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
    if (-not [string]::IsNullOrWhiteSpace($PinnedImage)) {
        if ($Service -eq "all") {
            throw "-PinnedImage requires one explicit service."
        }
        $Arguments["PinnedImage"] = $PinnedImage
    }
    if ($Service -ne "all") {
        $Arguments["Services"] = @($Service)
    }
    if ($SkipBuild) {
        $Arguments["SkipBuild"] = $true
    }
    if ($Recreate -and $StackAction -eq "Prepare") {
        $Arguments["Recreate"] = $true
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
    if (-not (Test-Path -LiteralPath $ScaleToZeroStarter -PathType Leaf)) {
        throw "Scale-to-zero starter not found: $ScaleToZeroStarter"
    }
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Salad stack manifest not found: $ManifestPath"
    }

    foreach ($Name in Get-SelectedServiceNames) {
        Write-Host "=== Salad Start : $Name ===" -ForegroundColor Cyan
        $Arguments = @{
            Service = $Name
            EnvFile = $EnvFile
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
        "Salad scale-to-zero Start complete: services={0}" -f ((Get-SelectedServiceNames) -join ",")
    ) -ForegroundColor Green
}

function Invoke-ProtectedSmokeBootstrap {
    if ($Service -eq "all") {
        throw "Prewarm requires one explicit service so GPU workers stay serialized."
    }
    if (-not (Test-Path -LiteralPath $ProtectedSmokeBootstrap -PathType Leaf)) {
        throw "Protected prewarm bootstrap not found: $ProtectedSmokeBootstrap"
    }

    Write-Host "=== Salad Prewarm : $Service ===" -ForegroundColor Cyan
    $Arguments = @{
        Service = $Service
        EnvFile = $EnvFile
        TimeoutMinutes = $PrewarmTimeoutMinutes
    }
    if ($NonInteractive) {
        $Arguments["NonInteractive"] = $true
    }
    & $ProtectedSmokeBootstrap @Arguments
    $CallSucceeded = $?
    if (-not $CallSucceeded) {
        throw "Salad prewarm failed for service '$Service'."
    }
}

function Invoke-ScaleToZeroRestore {
    if (-not (Test-Path -LiteralPath $ScaleToZeroRestorer -PathType Leaf)) {
        throw "Scale-to-zero restorer not found: $ScaleToZeroRestorer"
    }

    foreach ($Name in Get-SelectedServiceNames) {
        Write-Host "=== Salad Scale-to-Zero Restore : $Name ===" -ForegroundColor Cyan
        $Arguments = @{
            Service = $Name
            EnvFile = $EnvFile
        }
        if ($NonInteractive) {
            $Arguments["NonInteractive"] = $true
        }
        & $ScaleToZeroRestorer @Arguments
        $CallSucceeded = $?
        if (-not $CallSucceeded) {
            throw "Salad scale-to-zero restore failed for service '$Name'."
        }
    }
}

function Invoke-ZeroReplicaGuard {
    if (-not (Test-Path -LiteralPath $ZeroReplicaGuard -PathType Leaf)) {
        throw "Zero-replica guard not found: $ZeroReplicaGuard"
    }

    foreach ($Name in Get-SelectedServiceNames) {
        Write-Host "=== Salad Zero-Replica Guard : $Name ===" -ForegroundColor Cyan
        $Arguments = @{
            Service = $Name
            EnvFile = $EnvFile
        }
        if ($NonInteractive) {
            $Arguments["NonInteractive"] = $true
        }
        & $ZeroReplicaGuard @Arguments
        $CallSucceeded = $?
        if (-not $CallSucceeded) {
            throw "Salad zero-replica guard failed for service '$Name'."
        }
    }
}

function Invoke-ZeroReplicaFallback {
    param([Parameter(Mandatory)][object]$StopFailure)

    Write-Warning (
        "Salad stack Stop reported an error. The zero-replica guard will still verify the " +
        "terminal stopped/replicas=0 state. Error: $($StopFailure.Exception.Message)"
    )
    try {
        Invoke-ZeroReplicaGuard
    }
    catch {
        throw (
            "Salad Stop failed and the zero-replica fallback also failed. Stop error: " +
            "$($StopFailure.Exception.Message) Guard error: $($_.Exception.Message)"
        )
    }
    Write-Warning (
        "Salad Stop reported an error, but the zero-replica guard verified stopped/replicas=0."
    )
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
        python scripts/smoke/run_salad_smoke_suite.py `
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
        Invoke-ProtectedSmokeBootstrap
        Invoke-Smoke
    }
    finally {
        try {
            Invoke-ScaleToZeroRestore
        }
        finally {
            try {
                Invoke-StackAction -StackAction "Stop"
            }
            catch {
                Invoke-ZeroReplicaFallback -StopFailure $_
            }
        }
    }
}

function Invoke-SafeStop {
    try {
        Invoke-ScaleToZeroRestore
    }
    finally {
        try {
            Invoke-StackAction -StackAction "Stop"
        }
        catch {
            Invoke-ZeroReplicaFallback -StopFailure $_
        }
    }
}

function Invoke-SafePrewarm {
    try {
        Invoke-ProtectedSmokeBootstrap
    }
    finally {
        Invoke-SafeStop
    }

    Write-Host (
        "Salad prewarm readiness validation passed and the worker was returned to " +
        "stopped/replicas=0."
    ) -ForegroundColor Green
}


if ($Recreate -and $Action -ne "Prepare") {
    throw "-Recreate is only valid with -Action Prepare."
}
if ($Recreate -and $Service -eq "all") {
    throw "-Recreate requires one explicit service; refusing to recreate the full stack."
}

switch ($Action) {
    "Validate" { Invoke-StackAction -StackAction "Validate" }
    "Prepare" { Invoke-StackAction -StackAction "Prepare" }
    "Start" { Invoke-ScaleToZeroStart }
    "Prewarm" { Invoke-SafePrewarm }
    "Status" { Invoke-StackAction -StackAction "Status" }
    "Smoke" { Invoke-Smoke }
    "ProtectedSmoke" { Invoke-ProtectedSmoke }
    "Stop" { Invoke-SafeStop }
}
