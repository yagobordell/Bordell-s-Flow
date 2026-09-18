from pathlib import Path


def test_phase6_routes_safety_fallback_to_flux2_klein_keyframe_queue() -> None:
    script = Path("scripts/run_phase6_keyframes.py").read_text(encoding="utf-8")

    assert "SaladFlux2KleinImageProvider" in script
    assert "FLUX2_KLEIN_KEYFRAME_TASK" in script
    assert "settings.salad_flux2_klein_queue_name" in script
    assert "settings.flux2_klein_model" in script
    assert "flux2-klein-keyframe-client" in script
    assert "fallback_model=args.fallback_model" in script


def test_phase6_controlled_cleanup_restores_flux2_scale_to_zero() -> None:
    script = Path("scripts/run_phase6_keyframes_controlled.ps1").read_text(encoding="utf-8")

    assert "start_salad_flux2_klein_prewarm.ps1" in script
    assert "restore_salad_flux2_klein_scale_to_zero.ps1" in script
    assert "-Service flux2_klein" in script
