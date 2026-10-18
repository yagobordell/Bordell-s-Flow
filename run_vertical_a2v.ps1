# Direct 9:16 avatar video from a JPEG/PNG and MP3/WAV; independent of run.ps1 B pipeline.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Image,
    [Parameter(Mandatory)][string]$Audio,
    [string]$Script = "",
    [string]$OutputDir = "",
    [string]$Prompt = "",
    [long]$Seed = 4242,
    [string]$RunTag = "default",
    [switch]$PlanOnly,
    [switch]$NativeOutput,
    [switch]$RetryCancelled
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    $arguments = @(
        "run", "--locked", "--extra", "dev", "python",
        "scripts/pipeline/run_vertical_a2v.py", "--image", $Image, "--audio", $Audio,
        "--seed", "$Seed", "--run-tag", $RunTag
    )
    if ($Script) { $arguments += @("--script", $Script) }
    if ($OutputDir) { $arguments += @("--output-dir", $OutputDir) }
    if ($Prompt) { $arguments += @("--prompt", $Prompt) }
    if ($PlanOnly) { $arguments += "--plan-only" }
    if ($NativeOutput) { $arguments += "--native-output" }
    if ($RetryCancelled) { $arguments += "--retry-cancelled" }
    & uv @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
