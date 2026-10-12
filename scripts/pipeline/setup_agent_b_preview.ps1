# Prepare a standalone Python 3.12 Agent B virtual environment on Windows.
# No API calls, voice, images or video rendering.
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Push-Location $repo
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is missing. Install uv 0.12.18 and rerun."
    }
    uv venv --python 3.12 .venv-agent-b
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Agent B preview environment." }
    $python = Join-Path $repo ".venv-agent-b/Scripts/python.exe"
    uv pip install --python $python -e ".[dev]" -r config/agent-b-preview-requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Agent B preview dependency installation failed." }
    uv pip check --python $python
    if ($LASTEXITCODE -ne 0) { throw "Agent B preview dependencies are inconsistent." }
    & $python -m scripts.pipeline.run_agent_b_preview --preflight `
        --input examples/agent_b_preview_phone.json `
        --project .video_factory_work/agent_b_preview_phone --model gpt-5.1
    if ($LASTEXITCODE -ne 0) { throw "Agent B preview preflight failed." }
    Write-Host "Agent B preview ready. Select .venv-agent-b/Scripts/python.exe in VS Code."
} finally {
    Pop-Location
}
