from pathlib import Path

PHASE4_SCRIPT = Path("scripts/run_phase4_assets.py")
PHASE6_SCRIPT = Path("scripts/run_phase6_keyframes.py")
PHASE4_CONTROLLED = Path("scripts/run_phase4_assets_controlled.ps1")
PHASE6_CONTROLLED = Path("scripts/run_phase6_keyframes_controlled.ps1")
VALIDATION_MANAGER = Path("scripts/manage_salad_validation.ps1")


def test_phase4_assets_accepts_utf8_bom() -> None:
    text = PHASE4_SCRIPT.read_text(encoding="utf-8")
    assert 'read_text(encoding="utf-8-sig")' in text


def test_qwen_clients_use_cold_start_pending_budget() -> None:
    for script in (PHASE4_SCRIPT, PHASE6_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "DEFAULT_QWEN_PENDING_TIMEOUT_SECONDS = 1800.0" in text
        assert "default=DEFAULT_QWEN_PENDING_TIMEOUT_SECONDS" in text
        assert "cold Qwen worker" in text
        assert "ideogram" not in text.lower()
        assert "flux" not in text.lower()


def test_validation_manager_exposes_explicit_prewarm_action() -> None:
    text = VALIDATION_MANAGER.read_text(encoding="utf-8")
    assert '"Prewarm"' in text
    assert '[int]$PrewarmTimeoutMinutes = 90' in text
    assert 'TimeoutMinutes = $PrewarmTimeoutMinutes' in text
    assert '"Prewarm" { Invoke-ProtectedSmokeBootstrap }' in text


def test_controlled_qwen_runners_prewarm_before_queue_and_cleanup() -> None:
    for script in (PHASE4_CONTROLLED, PHASE6_CONTROLLED):
        text = script.read_text(encoding="utf-8")
        assert "start_salad_optimized_prewarm.ps1" in text
        assert 'Service = "qwen_image_21"' in text
        assert "TimeoutMinutes = $PrewarmTimeoutMinutes" in text
        assert '"--pending-timeout-seconds", $PendingTimeoutSeconds' in text
        assert "restore_salad_scale_to_zero.ps1" in text
        assert "cleanup_salad_queue.ps1" in text
        assert "finally {" in text
        assert "ideogram" not in text.lower()
        assert "flux" not in text.lower()
        assert text.index("start_salad_optimized_prewarm.ps1") < text.index(
            '"--pending-timeout-seconds"'
        )
