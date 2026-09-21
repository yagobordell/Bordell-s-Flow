from pathlib import Path

PYTHON_SMOKE = Path("scripts/run_flux2_klein_smoke.py")
CONTROLLED_SMOKE = Path("scripts/run_flux2_klein_smoke_controlled.ps1")
PREWARM = Path("scripts/start_salad_flux_prewarm.ps1")
COLLECTOR = Path("scripts/collect_flux2_klein_salad_metrics.ps1")


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
    assert '"collect_flux2_klein_salad_metrics.ps1"' in text
    assert "-StartTimeUtc" in text
    assert "-EndTimeUtc" in text
    assert "-WindowMinutes 2" in text
    assert "-RetryCount 3" in text


def test_flux2_klein_prewarm_reports_pull_and_bootstrap_timings() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "FLUX2_KLEIN_PREWARM_METRIC" in text
    assert "assignment_seconds=" in text
    assert "container_started_seconds=" in text
    assert "image_pull_and_start_seconds=" in text
    assert "ready_seconds=" in text
    assert "bootstrap_after_start_seconds=" in text
    assert "running-not-ready watchdog" in text
    assert "Request-InstanceReallocation" in text
    assert "$MaxNodeReallocations = 1" in text


def test_flux2_klein_historical_metric_collector_never_starts_gpu() -> None:
    text = COLLECTOR.read_text(encoding="utf-8")

    assert "/log-entries" in text
    assert '"FLUX2_KLEIN_RUNTIME_READY"' in text
    assert '"FLUX2_KLEIN_INFERENCE_METRIC"' in text
    assert 'log contains "' in text
    assert 'sort_order = "desc"' in text
    assert "WindowMinutes = 2" in text
    assert "RetryCount = 3" in text
    assert "page_size = 25" in text
    assert "Invoke-SaladMarkerQuery" in text
    assert "FLUX2_KLEIN_HISTORICAL_METRICS" in text
    assert "FLUX2_KLEIN_SMOKE_REPORT" in text
    assert "/start" not in text
    assert "replicas = 1" not in text
