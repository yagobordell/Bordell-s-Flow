from pathlib import Path

SMOKE = Path("scripts/run_flux2_fallback_smoke.py")


def test_fallback_smoke_uses_one_terminal_primary_rejection_and_real_flux2_provider() -> None:
    script = SMOKE.read_text(encoding="utf-8")

    assert "TerminalSafetyPrimary" in script
    assert "Ideogram 4 safety filter blocked all provider caption variants" in script
    assert "SaladFlux2KleinImageProvider" in script
    assert "SafetyFallbackImageProvider" in script
    assert "fallback_model=FLUX2_KLEIN_MODEL_ID" in script
    assert "if primary.calls != 1:" in script
    assert 'queue_name=settings.salad_flux2_klein_queue_name' in script
    assert '"flux2-fallback-e2e.png"' in script
