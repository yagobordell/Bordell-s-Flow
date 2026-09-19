from pathlib import Path

SCRIPT = Path("scripts/run_phase4_assets_controlled.ps1")
PYTHON_RUNNER = Path("scripts/run_phase4_assets.py")


def test_phase4_defers_flux_until_fresh_ideogram_safety_rejection() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "$OnDemandFluxPrewarm = $IdeogramNeeded -and -not $FluxNeeded" in text
    assert "--prewarm-fallback-on-demand" in text
    assert '"start_salad_scale_to_zero.ps1"' not in text
    assert 'if ($IdeogramNeeded) {\n        $IdeogramTouched = $true' in text


def test_phase4_on_demand_fallback_prewarms_ready_flux_before_submission() -> None:
    text = PYTHON_RUNNER.read_text(encoding="utf-8")

    assert '"start_salad_flux_prewarm.ps1"' in text
    assert "Ideogram safety rejection confirmed; prewarming FLUX" in text
    assert "await asyncio.to_thread(subprocess.run, command, check=True)" in text


def test_phase4_keeps_deterministic_flux_prewarm_for_known_safety_blocks() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "if ($FluxNeeded)" in text
    assert "prewarm one ready replica before queue submission" in text
    assert "& $FluxPrewarm @FluxPrewarmArguments" in text
