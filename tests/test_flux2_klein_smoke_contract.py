from pathlib import Path

PYTHON_SMOKE = Path("scripts/run_flux2_klein_smoke.py")
CONTROLLED_SMOKE = Path("scripts/run_flux2_klein_smoke_controlled.ps1")


def test_flux2_klein_smoke_exercises_queue_r2_and_png_contract() -> None:
    text = PYTHON_SMOKE.read_text(encoding="utf-8")

    assert "SaladFlux2KleinImageProvider" in text
    assert "InferenceJobExecutor" in text
    assert "create_r2_storage" in text
    assert "Image.open" in text
    assert '"FLUX2_KLEIN_SMOKE "' in text
    assert '"model_revision"' in text
    assert '"png_size_bytes"' in text
    assert '"request_sha256"' in text


def test_controlled_flux2_klein_smoke_always_restores_scale_to_zero() -> None:
    text = CONTROLLED_SMOKE.read_text(encoding="utf-8")

    assert '"start_salad_flux_prewarm.ps1"' in text
    assert '"run_flux2_klein_smoke.py"' in text
    assert '"restore_salad_flux_scale_to_zero.ps1"' in text
    assert '"cleanup_salad_queue.ps1"' in text
    assert 'Service = "flux2_klein"' in text
    assert "finally {" in text
    assert "$PrimaryFailure" in text
    assert "preserving the original FLUX.2 Klein smoke failure" in text
