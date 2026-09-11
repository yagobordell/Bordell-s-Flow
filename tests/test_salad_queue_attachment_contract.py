from pathlib import Path

REPAIR_SCRIPT = Path("scripts/repair_salad_queue_attachment.ps1")


def test_queue_attachment_verifies_without_enabling_networking() -> None:
    script = REPAIR_SCRIPT.read_text(encoding="utf-8")

    assert "queue_connection.queue_name" in script
    assert "queue_autoscaler.min_replicas" in script
    assert "function New-Networking" not in script
    assert "networking = New-Networking" not in script


def test_queue_attachment_uses_versioned_names_instead_of_recreation() -> None:
    script = REPAIR_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$AllowMissing" in script
    assert "has no existing container group; preflight repair not needed" in script
    assert "Cancel them before Prepare can change the container group" in script
    assert "-Method Delete" not in script
    assert "cannot be safely recreated with the same Salad name" in script
    assert "Increment services.$Service.group_name" in script
