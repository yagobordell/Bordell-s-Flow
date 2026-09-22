[CmdletBinding()]
param(
    [string]$AvatarImage = "",

    [string]$Audio = "",

    [string]$AvatarInputDir = "data/input/avatar",

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

function Resolve-UniqueMediaPath {
    param(
        [Parameter(Mandatory)][string]$Directory,
        [Parameter(Mandatory)][string[]]$Extensions,
        [Parameter(Mandatory)][string]$Kind
    )

    $ResolvedDirectory = $Directory
    if (-not [IO.Path]::IsPathRooted($ResolvedDirectory)) {
        $ResolvedDirectory = Join-Path $RepoRoot $ResolvedDirectory
    }
    $ResolvedDirectory = [IO.Path]::GetFullPath($ResolvedDirectory)

    if (-not (Test-Path -LiteralPath $ResolvedDirectory -PathType Container)) {
        throw "Avatar input directory not found: $ResolvedDirectory"
    }

    $Candidates = @(
        Get-ChildItem -LiteralPath $ResolvedDirectory -File |
            Where-Object { $Extensions -contains $_.Extension.ToLowerInvariant() }
    )
    if ($Candidates.Count -eq 0) {
        throw "No $Kind file found in $ResolvedDirectory. Supported extensions: $($Extensions -join ', ')"
    }
    if ($Candidates.Count -gt 1) {
        $Names = ($Candidates | ForEach-Object { $_.Name }) -join ", "
        throw "Expected exactly one $Kind file in $ResolvedDirectory, found $($Candidates.Count): $Names"
    }

    return $Candidates[0].FullName
}

foreach ($RequiredPath in @($Manager, $Bootstrap, $Submit)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required A2V smoke helper not found: $RequiredPath"
    }
}

Import-EnvFile -Path $EnvFile

$AudioExtensions = @(".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")
$ImageExtensions = @(".png", ".jpg", ".jpeg", ".webp")

if ([string]::IsNullOrWhiteSpace($Audio)) {
    $Audio = Resolve-UniqueMediaPath `
        -Directory $AvatarInputDir `
        -Extensions $AudioExtensions `
        -Kind "audio"
    Write-Host "Using avatar test audio: $Audio" -ForegroundColor Green
}

if ([string]::IsNullOrWhiteSpace($AvatarImage)) {
    $AvatarImage = Resolve-UniqueMediaPath `
        -Directory $AvatarInputDir `
        -Extensions $ImageExtensions `
        -Kind "image"
    Write-Host "Using avatar test image: $AvatarImage" -ForegroundColor Green
}

$ResolvedAudio = [IO.Path]::GetFullPath(
    $(if ([IO.Path]::IsPathRooted($Audio)) { $Audio } else { Join-Path $RepoRoot $Audio })
)
if (-not (Test-Path -LiteralPath $ResolvedAudio -PathType Leaf)) {
    throw "Speech audio not found: $ResolvedAudio"
}

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

    $CleanupArgs = @(
        $Submit,
        "--audio", $ResolvedAudio,
        "--avatar-image", $ResolvedAvatar,
        "--output-dir", (Join-Path $RepoRoot $OutputDir),
        "--cleanup-stale-only"
    )
    if (-not [string]::IsNullOrWhiteSpace($Prompt)) {
        $CleanupArgs += @("--prompt", $Prompt)
    }
    Write-Host "=== Clear stale A2V transport for this exact input pair ===" -ForegroundColor Cyan
    & python @CleanupArgs
    if (-not $?) {
        throw "LTX A2V stale transport cleanup failed."
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
    $PythonArgs += @("--avatar-image", $ResolvedAvatar)
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
}
