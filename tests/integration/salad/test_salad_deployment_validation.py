from pathlib import Path

WORKER_MANAGER = Path("scripts/salad/manage_salad_worker.ps1")


def test_worker_manager_keeps_expensive_actions_explicit() -> None:
    text = WORKER_MANAGER.read_text(encoding="utf-8")

    assert 'ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")' in text
    assert "capacity.start_replicas" in text
    assert "capacity.max_replicas" in text
    assert "set explicit replica capacity" in text
    assert "Wait-ForStoppedGroup" in text
    assert "Wait-ForStoppedZeroReplicas" not in text
    assert "queue_autoscaler =" not in text
    assert "queue_connection =" not in text


def test_worker_manager_exposes_targeted_prepare_recreate_and_pinned_image() -> None:
    text = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "[switch]$Recreate" in text
    assert "[string]$PinnedImage" in text
    assert "-Recreate is only valid with Prepare." in text
    assert "Remove-StoppedContainerGroup" in text
