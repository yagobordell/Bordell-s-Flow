from pathlib import Path

PHASE4_SCRIPT = Path("scripts/run_phase4_assets.py")
PHASE6_SCRIPT = Path("scripts/run_phase6_keyframes.py")
PHASE4_CONTROLLED = Path("scripts/run_phase4_assets_controlled.ps1")
PHASE6_CONTROLLED = Path("scripts/run_phase6_keyframes_controlled.ps1")
VALIDATION_MANAGER = Path("scripts/manage_salad_validation.ps1")


def test_phase4_assets_accepts_utf8_bom() -> None:
    text = PHASE4_SCRIPT.read_text(encoding="utf-8")
    assert 'read_text(encoding="utf-8-sig")' in text


def test_ideogram_clients_fail_fast_if_a_ready_worker_does_not_claim_job() -> None:
    for script in (PHASE4_SCRIPT, PHASE6_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS = 300.0" in text
        assert "default=DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS" in text
        assert "already-prewarmed Ideogram worker" in text


def test_validation_manager_exposes_explicit_prewarm_action() -> None:
    text = VALIDATION_MANAGER.read_text(encoding="utf-8")
    assert '"Prewarm"' in text
    assert '[int]$PrewarmTimeoutMinutes = 90' in text
    assert 'TimeoutMinutes = $PrewarmTimeoutMinutes' in text
    assert '"Prewarm" { Invoke-ProtectedSmokeBootstrap }' in text


def test_controlled_ideogram_runners_use_optimized_prewarm_before_queue() -> None:
    for script in (PHASE4_CONTROLLED, PHASE6_CONTROLLED):
        text = script.read_text(encoding="utf-8")
        assert 'start_salad_optimized_prewarm.ps1' in text
        assert 'Service = "ideogram4"' in text
        assert 'TimeoutMinutes = $PrewarmTimeoutMinutes' in text
        assert '"--pending-timeout-seconds", $PendingTimeoutForRun' in text
        assert 'AI_VIDEO_FACTORY_SCALE_TO_ZERO_FALLBACK' in text
        assert '$PreferFlux = $PreferFallbackProvider' in text
        assert 'prefer-fallback-provider' in text
        assert '[int]$PendingTimeoutSeconds = 300' in text
        assert 'finally {' in text
        assert '-Action Stop' in text
        assert '-Action Status' in text
        assert text.index('start_salad_optimized_prewarm.ps1') < text.index(
            '"--pending-timeout-seconds"'
        )
