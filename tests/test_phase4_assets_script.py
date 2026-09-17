from pathlib import Path

PHASE4_SCRIPT = Path("scripts/run_phase4_assets.py")
PHASE6_SCRIPT = Path("scripts/run_phase6_keyframes.py")
PHASE4_CONTROLLED = Path("scripts/run_phase4_assets_controlled.ps1")
PHASE6_CONTROLLED = Path("scripts/run_phase6_keyframes_controlled.ps1")
VALIDATION_MANAGER = Path("scripts/manage_salad_validation.ps1")


def test_phase4_assets_accepts_utf8_bom() -> None:
    text = PHASE4_SCRIPT.read_text(encoding="utf-8")
    assert 'read_text(encoding="utf-8-sig")' in text


def test_ideogram_clients_keep_short_ready_worker_claim_timeout() -> None:
    for script in (PHASE4_SCRIPT, PHASE6_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS = 300.0" in text
        assert "default=DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS" in text
        assert "--pending-timeout-seconds" in text


def test_image_clients_expose_longer_scale_to_zero_flux_timeout() -> None:
    for script in (PHASE4_SCRIPT, PHASE6_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "--flux-pending-timeout-seconds" in text
        assert "settings.flux_fallback_pending_timeout_seconds" in text
        assert "SaladFluxSchnellImageProvider" in text
        assert "SafetyFallbackImageProvider" in text


def test_validation_manager_exposes_explicit_prewarm_action() -> None:
    text = VALIDATION_MANAGER.read_text(encoding="utf-8")
    assert '"Prewarm"' in text
    assert '[int]$PrewarmTimeoutMinutes = 90' in text
    assert 'TimeoutMinutes = $PrewarmTimeoutMinutes' in text
    assert '"Prewarm" { Invoke-ProtectedSmokeBootstrap }' in text


def test_controlled_image_runners_plan_before_gpu_and_cleanup_both_workers() -> None:
    for script in (PHASE4_CONTROLLED, PHASE6_CONTROLLED):
        text = script.read_text(encoding="utf-8")
        assert 'start_salad_optimized_prewarm.ps1' in text
        assert '$NeedsIdeogram' in text
        assert '$NeedsFlux' in text
        assert 'primary_status -eq "miss"' in text
        assert 'status -eq "safety_blocked"' in text
        assert 'Service = "ideogram4"' in text
        assert '--pending-timeout-seconds $PendingTimeoutSeconds' in text
        assert '--flux-pending-timeout-seconds $FluxPendingTimeoutSeconds' in text
        assert '[int]$PendingTimeoutSeconds = 300' in text
        assert '[int]$FluxPendingTimeoutSeconds = 1800' in text
        assert '-Service flux_schnell' in text
        assert 'cleanup_salad_queue.ps1' in text
        assert 'finally {' in text
        assert '-Action Stop' in text
        assert '-Action Status' in text


def test_phase4_controlled_runner_audits_negative_cache_before_ideogram_prewarm() -> None:
    text = PHASE4_CONTROLLED.read_text(encoding="utf-8")

    assert 'audit_phase4_reference_cache.py' in text
    assert text.index('& python $Audit') < text.index('& $OptimizedPrewarm')
    assert "known safety blocks will cold-start FLUX only" in text


def test_phase6_controlled_runner_audits_negative_cache_before_ideogram_prewarm() -> None:
    text = PHASE6_CONTROLLED.read_text(encoding="utf-8")

    assert 'audit_phase6_keyframe_cache.py' in text
    assert text.index('& python $Audit') < text.index('& $OptimizedPrewarm')
    assert "known safety blocks will cold-start FLUX only" in text
