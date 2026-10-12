# Managed one-command Video Factory launcher.
# YouTube libraries are installed only when YOUTUBE_ENABLED is explicitly true.
$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    # Match pydantic-settings precedence: later app.env files override earlier
    # nonempty entries; an explicit process environment variable wins.
    $youtubeValue = ""
    foreach ($name in @(".env", "config/app.env", "config/salad.env")) {
        if (Test-Path -LiteralPath $name -PathType Leaf) {
            foreach ($line in (Get-Content -LiteralPath $name)) {
                if ($line -match '^\s*YOUTUBE_ENABLED\s*=\s*([^\s#]+)') {
                    $youtubeValue = $Matches[1].Trim("'", '"')
                }
            }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($env:YOUTUBE_ENABLED)) {
        $youtubeValue = $env:YOUTUBE_ENABLED
    }
    if (@($args) -contains "--agent-b") {
        # Explicit opt-in uses the separately installed SDK; never change uv.lock.
        $previewPython = Join-Path $PSScriptRoot ".venv-agent-b/Scripts/python.exe"
        if (-not (Test-Path -LiteralPath $previewPython -PathType Leaf)) {
            throw "Agent B environment missing. Run .\\scripts\\pipeline\\setup_agent_b_preview.ps1 first."
        }
        & $previewPython scripts/pipeline/run_managed.py @args
        $exitCode = $LASTEXITCODE
    }
    else {
        $uvArgs = @("run", "--locked", "--extra", "dev")
        if ($youtubeValue -match '^(?i:true|1|yes|on)$') {
            $uvArgs += @("--extra", "youtube")
        }
        & uv @uvArgs python scripts/pipeline/run_managed.py @args
        $exitCode = $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

exit $exitCode
