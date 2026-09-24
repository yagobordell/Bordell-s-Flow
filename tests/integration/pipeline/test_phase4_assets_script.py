from pathlib import Path

PHASE4_SCRIPT = Path("scripts/pipeline/run_phase4_assets.py")
PHASE6_SCRIPT = Path("scripts/pipeline/run_phase6_keyframes.py")
PHASE4_CONTROLLED = Path("scripts/pipeline/run_phase4_assets_controlled.ps1")
PHASE6_CONTROLLED = Path("scripts/pipeline/run_phase6_keyframes_controlled.ps1")
VALIDATION_MANAGER = Path("scripts/salad/manage_salad_validation.ps1")


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


def test_validation_manager_exposes_explicit_prewarm_action() -> None:
    text = VALIDATION_MANAGER.read_text(encoding="utf-8")
    assert '"Prewarm"' in text
    assert '[int]$PrewarmTimeoutMinutes = 90' in text
    assert 'TimeoutMinutes = $PrewarmTimeoutMinutes' in text
    assert '"Prewarm" { Invoke-SafePrewarm }' in text



def test_controlled_qwen_runners_prewarm_before_queue_and_cleanup() -> None:
    shared = Path("scripts/pipeline/_qwen_controlled.ps1").read_text(encoding="utf-8")
    for script in (PHASE4_CONTROLLED, PHASE6_CONTROLLED):
        wrapper = script.read_text(encoding="utf-8")
        assert "_qwen_controlled.ps1" in wrapper
        assert '"--pending-timeout-seconds", $PendingTimeoutSeconds' in wrapper
        assert "ideogram" not in wrapper.lower()

    assert "start_salad_optimized_prewarm.ps1" in shared
    assert 'Service = "qwen_image_21"' in shared
    assert "TimeoutMinutes = $PrewarmTimeoutMinutes" in shared
    assert "manage_salad_validation.ps1" in shared
    assert "-Action Stop -Service qwen_image_21" in shared
    assert "cleanup_salad_queue.ps1" in shared
    assert "finally {" in shared
    assert shared.index("Qwen-Image-2.1 prewarm") < shared.rindex(
        "& python $RunnerScript @RunnerArguments"
    )
    assert shared.index("-Action Stop") < shared.index("& $QueueCleanup")
    assert shared.index("& $QueueCleanup") < shared.index("-Action Status")
