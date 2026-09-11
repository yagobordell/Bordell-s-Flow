from pathlib import Path

REPAIR_SCRIPT = Path("scripts/repair_salad_queue_attachment.ps1")


def test_queue_attachment_does_not_enable_networking() -> None:
    script = REPAIR_SCRIPT.read_text(encoding="utf-8")

    assert "queue_connection = New-QueueConnection" in script
    assert "queue_autoscaler = New-QueueAutoscaler" in script
    assert "function New-Networking" not in script
    assert "networking = New-Networking" not in script


def test_queue_attachment_can_recover_after_failed_recreation() -> None:
    script = REPAIR_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$AllowMissing" in script
    assert "has no existing container group; preflight repair not needed" in script
    assert "Cancel them before Prepare can change the container group" in script
