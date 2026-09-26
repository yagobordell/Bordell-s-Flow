# Short Windows PowerShell entrypoint for the existing B1.1 -> B1.2 -> B2 runner.
# Usage: .\run.ps1 test2 Jorge --no-image --no-audio
# Audio-only regeneration: .\run.ps1 test2 --regenerate-audio
# Defaults here are intentionally local to the shortcut; the Python CLI remains compatible.
$ErrorActionPreference = "Stop"

$runnerArgs = @("--scripts-dir", "data/input/scripts")
$remaining = @($args)

if ($remaining.Count -gt 0 -and -not $remaining[0].StartsWith("-")) {
    $runnerArgs += @("--script", $remaining[0])
    $remaining = @($remaining | Select-Object -Skip 1)

    if ($remaining.Count -gt 0 -and -not $remaining[0].StartsWith("-")) {
        $runnerArgs += @("--avatar", $remaining[0])
        $remaining = @($remaining | Select-Object -Skip 1)
    }
}

$runnerArgs += $remaining

Push-Location $PSScriptRoot
try {
    & uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py @runnerArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
