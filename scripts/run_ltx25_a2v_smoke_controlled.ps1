[CmdletBinding()]
param(
    [string]$AvatarImage = "",

    [string]$Audio = "",

    [string]$Prompt = "",

    [string]$EnvFile = ".env",

    [string]$OutputDir = "data/output/deployment-validation/ltx25-a2v",

    [switch]$SkipPrepare,

    [switch]$SkipBuild,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Manager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
$Bootstrap = Join-Path $PSScriptRoot "start_salad_protected_smoke.ps1"
$Submit = Join-Path $PSScriptRoot "submit_ltx25_a2v_smoke.py"
$Service = "ltx25"

function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path $RepoRoot $Resolved
    }
    if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
        return
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
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name))) {
            [Environment]::SetEnvironmentVariable($Name, $Value)
        }
    }
}

foreach ($RequiredPath in @($Manager, $Bootstrap, $Submit)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required A2V smoke helper not found: $RequiredPath"
    }
}

Import-EnvFile -Path $EnvFile

$GeneratedAudio = $false
$RunningOnWindows = [string]$env:OS -eq "Windows_NT"
if ([string]::IsNullOrWhiteSpace($Audio)) {
    if (-not $RunningOnWindows) {
        throw "A speech WAV is required on non-Windows hosts. Pass -Audio <path>."
    }
    $FixtureDir = Join-Path $RepoRoot "data\output\deployment-validation\ltx25-a2v\fixtures"
    New-Item -ItemType Directory -Force -Path $FixtureDir | Out-Null
    $Audio = Join-Path $FixtureDir "speech-smoke.wav"
    Add-Type -AssemblyName System.Speech
    $Synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $Synth.SetOutputToWaveFile($Audio)
        $Synth.Speak(
            "This is a short L T X avatar synchronization test with natural spoken audio."
        )
    }
    finally {
        $Synth.Dispose()
    }
    $GeneratedAudio = $true
    Write-Host "Generated reusable speech fixture: $Audio" -ForegroundColor Green
}

$ResolvedAudio = [IO.Path]::GetFullPath(
    $(if ([IO.Path]::IsPathRooted($Audio)) { $Audio } else { Join-Path $RepoRoot $Audio })
)
if (-not (Test-Path -LiteralPath $ResolvedAudio -PathType Leaf)) {
    throw "Speech audio not found: $ResolvedAudio"
}

$ResolvedAvatar = ""
if (-not [string]::IsNullOrWhiteSpace($AvatarImage)) {
    $ResolvedAvatar = [IO.Path]::GetFullPath(
        $(if ([IO.Path]::IsPathRooted($AvatarImage)) {
            $AvatarImage
        }
        else {
            Join-Path $RepoRoot $AvatarImage
        })
    )
    if (-not (Test-Path -LiteralPath $ResolvedAvatar -PathType Leaf)) {
        throw "Avatar image not found: $ResolvedAvatar"
    }
}

$Common = @{
    Service = $Service
    EnvFile = $EnvFile
}
if ($NonInteractive) {
    $Common["NonInteractive"] = $true
}

try {
    if (-not $SkipPrepare) {
        $Prepare = $Common.Clone()
        $Prepare["Action"] = "Prepare"
        if ($SkipBuild) {
            $Prepare["SkipBuild"] = $true
        }
        Write-Host "=== Prepare LTX 2.5 I2V+A2V worker ===" -ForegroundColor Cyan
        & $Manager @Prepare
        if (-not $?) {
            throw "LTX A2V worker Prepare failed."
        }
    }

    $BootstrapArgs = @{
        Service = $Service
        EnvFile = $EnvFile
        TimeoutMinutes = 60
        RunningNotReadyTimeoutMinutes = 60
    }
    if ($NonInteractive) {
        $BootstrapArgs["NonInteractive"] = $true
    }
    Write-Host "=== Start one protected RTX 5090 LTX worker ===" -ForegroundColor Cyan
    & $Bootstrap @BootstrapArgs
    if (-not $?) {
        throw "LTX A2V protected bootstrap failed."
    }

    $PythonArgs = @(
        $Submit,
        "--audio", $ResolvedAudio,
        "--output-dir", (Join-Path $RepoRoot $OutputDir)
    )
    if (-not [string]::IsNullOrWhiteSpace($ResolvedAvatar)) {
        $PythonArgs += @("--avatar-image", $ResolvedAvatar)
    }
    if (-not [string]::IsNullOrWhiteSpace($Prompt)) {
        $PythonArgs += @("--prompt", $Prompt)
    }

    Write-Host "=== Real image + speech -> avatar video smoke ===" -ForegroundColor Cyan
    & python @PythonArgs
    if (-not $?) {
        throw "LTX A2V real smoke failed."
    }
}
finally {
    Write-Host "=== Cost-guarded LTX cleanup ===" -ForegroundColor Cyan
    $Stop = $Common.Clone()
    $Stop["Action"] = "Stop"
    & $Manager @Stop
    if (-not $?) {
        throw "LTX A2V cleanup failed."
    }
    if ($GeneratedAudio) {
        Write-Host "Speech fixture retained for repeatable future smokes: $ResolvedAudio"
    }
}
