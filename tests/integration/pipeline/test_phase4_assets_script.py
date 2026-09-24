from pathlib import Path

PHASE4_SCRIPT = Path("scripts/pipeline/run_phase4_assets.py")
PHASE6_SCRIPT = Path("scripts/pipeline/run_phase6_keyframes.py")
PHASE4_CONTROLLED = Path("scripts/pipeline/run_phase4_assets_controlled.ps1")
PHASE6_CONTROLLED = Path("scripts/pipeline/run_phase6_keyframes_controlled.ps1")
SHARED_CONTROLLED = Path("scripts/pipeline/_qwen_controlled.ps1")


def test_phase4_assets_accepts_utf8_bom() -> None:
    text = PHASE4_SCRIPT.read_text(encoding="utf-8")
    assert 'read_text(encoding="utf-8-sig")' in text


def test_qwen_clients_use_cold_start_pending_budget() -> None:
    for script in (PHASE4_SCRIPT, PHASE6_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "DEFAULT_QWEN_PENDING_TIMEOUT_SECONDS = 1800.0" in text
        assert "default=DEFAULT_QWEN_PENDING_TIMEOUT_SECONDS" in text
        assert "cold Qwen worker" in text
        assert "PostgresJobQueueClient" in text
        assert "SaladJobQueueClient" not in text
        assert "ideogram" not in text.lower()


def test_controlled_qwen_runners_cache_before_explicit_capacity() -> None:
    shared = SHARED_CONTROLLED.read_text(encoding="utf-8")
    for script in (PHASE4_CONTROLLED, PHASE6_CONTROLLED):
        wrapper = script.read_text(encoding="utf-8")
        assert "_qwen_controlled.ps1" in wrapper
        assert '"--pending-timeout-seconds", $PendingTimeoutSeconds' in wrapper
        assert "ideogram" not in wrapper.lower()

    assert "manage_salad_worker.ps1" in shared
    assert 'Action = "Start"; Service = "qwen_image_21"; Replicas = 1' in shared
    assert 'Action = "Stop"; Service = "qwen_image_21"' in shared
    assert "start_salad_optimized_prewarm.ps1" not in shared
    assert "cleanup_salad_queue.ps1" not in shared
    assert "finally {" in shared
    assert shared.index("$CacheAuditScript") < shared.index('Action = "Start"')
    assert shared.index('Action = "Start"') < shared.rindex(
        "& python $RunnerScript @RunnerArguments"
    )
