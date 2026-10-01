# Avatar-first layout: drop a new PNG and TXT into data/ once.
# Thereafter, put loose pending TXT files inside data/<avatar>/.
# Usage: .\run.ps1 guion Jorge [--no-image | --no-video | --generate-video]
# Previous standalone B-pipeline behavior: .\run.ps1 --legacy-layout ...
$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    & uv run --locked --extra dev python scripts/pipeline/run_managed.py @args
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
