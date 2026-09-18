from pathlib import Path

SCRIPT = Path("scripts/run_phase4_assets_controlled.ps1")


def test_phase4_arms_flux_group_before_fresh_ideogram_work() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    arm_guard = "if ($IdeogramNeeded -and -not $FluxNeeded)"
    arm_action = 'Action = "Start"\n    Service = "flux2_klein"'
    ideogram_prewarm = 'if ($IdeogramNeeded) {\n        $IdeogramTouched = $true'

    assert arm_guard in text
    assert arm_action in text
    assert text.index(arm_guard) < text.index(ideogram_prewarm)
    assert "arm scale-to-zero group for dynamic safety fallback" in text


def test_phase4_flux_arm_retries_transient_control_plane_failures() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "for ($Attempt = 1; $Attempt -le 3; $Attempt += 1)" in text
    assert "Retrying idempotently" in text
    assert "Start-Sleep -Seconds 15" in text
    assert "FLUX.2 Klein scale-to-zero group could not be armed" in text


def test_phase4_keeps_deterministic_flux_prewarm_for_known_safety_blocks() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "if ($FluxNeeded)" in text
    assert "prewarm one ready replica before queue submission" in text
    assert "& $FluxPrewarm @FluxPrewarmArguments" in text
