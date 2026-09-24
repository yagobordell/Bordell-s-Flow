from pathlib import Path

SMOKE_SCRIPT = Path("scripts/smoke/run_salad_smoke_suite.py")
WORKER_MANAGER = Path("scripts/salad/manage_salad_worker.ps1")
STACK_MANAGER = Path("scripts/salad/manage_salad_stack.ps1")


def test_smoke_suite_uses_real_workers_and_postgres_transport() -> None:
    text = SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert '_SERVICE_ORDER = ("breeze_tts2", "whisper", "qwen_image_21", "ltx25")' in text
    assert "SaladBreezeSpeechProvider" in text
    assert "SaladWhisperTranscriptionProvider" in text
    assert "SaladQwenImage21Provider" in text
    assert "PostgresJobQueueClient" in text
    assert "scripts/pipeline/run_phase8_videos.py" in text
    assert "SaladJobQueueClient" not in text
    assert "submit_ltx25_smoke.py" not in text


def test_smoke_qwen_generation_matches_current_contract() -> None:
    text = SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert "QWEN_IMAGE_21_KEYFRAME_TASK" in text
    assert "QWEN_IMAGE_21_MODEL_ID" in text
    assert "size=QWEN_IMAGE_21_PRODUCTION_SIZE" in text
    assert 'output_format="png"' in text


def test_worker_manager_keeps_expensive_actions_explicit() -> None:
    text = WORKER_MANAGER.read_text(encoding="utf-8")

    assert 'ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")' in text
    assert "capacity.start_replicas" in text
    assert "capacity.max_replicas" in text
    assert "set explicit replica capacity" in text
    assert "Wait-ForStoppedZeroReplicas" in text
    assert "queue_autoscaler =" not in text
    assert "queue_connection =" not in text


def test_stack_manager_only_delegates_compute_lifecycle() -> None:
    text = STACK_MANAGER.read_text(encoding="utf-8")

    assert 'ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")' in text
    assert "$Document.stack.job_transport" in text
    assert "postgres" in text
    assert "start_salad_scale_to_zero.ps1" not in text
    assert "restore_salad_scale_to_zero.ps1" not in text
    assert "cleanup_salad_queue.ps1" not in text


def test_smoke_suite_persists_evidence_for_each_worker() -> None:
    text = SMOKE_SCRIPT.read_text(encoding="utf-8")

    for service in ("breeze_tts2", "whisper", "qwen_image_21", "ltx25"):
        assert f'_write_report(args.output_dir, "{service}"' in text
    assert '"smoke-summary.json"' in text


def test_worker_manager_exposes_targeted_prepare_recreate_and_pinned_image() -> None:
    text = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "[switch]$Recreate" in text
    assert "[string]$PinnedImage" in text
    assert "-Recreate is only valid with Prepare." in text
    assert "Remove-StoppedContainerGroup" in text
