from pathlib import Path

REPAIR_SCRIPT = Path("scripts/repair_salad_queue_attachment.ps1")


def test_queue_attachment_verifies_without_enabling_networking() -> None:
    script = REPAIR_SCRIPT.read_text(encoding="utf-8")

    assert "queue_connection.queue_name" in script
    assert "queue_autoscaler.min_replicas" in script
    assert "function New-Networking" not in script
    assert "networking = New-Networking" not in script


def test_stopped_group_can_repair_autoscaler_in_place() -> None:
    script = REPAIR_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$AllowMissing" in script
    assert "has no existing container group; preflight repair not needed" in script
    assert "Cancel pending/running jobs" in script
    assert "Terminal queue history will not block Prepare." in script
    assert "function Repair-GroupConfiguration" in script
    assert "Repairing Job Queue autoscaling in place" in script
    assert "queue_connection = New-QueueConnection" in script
    assert "queue_autoscaler = New-QueueAutoscaler" in script
    assert "-Method Patch" in script
    assert "-Method Delete" not in script
    assert "Runtime attachment will be validated after Start/Smoke." in script
