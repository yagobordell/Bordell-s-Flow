from pathlib import Path

REPAIR = Path("scripts/repair_salad_queue_attachment.ps1")
STACK = Path("scripts/manage_salad_stack.ps1")


def test_queue_attachment_repair_contract() -> None:
    text = REPAIR.read_text(encoding="utf-8")
    assert "queue_autoscaler" in text
    assert "queue_connection" in text
    assert "networking" in text
    assert "current_queue_length" in text
    assert "Method Delete" not in text
    assert "Method Patch" in text
    assert "replicas -ne 0" in text
    assert "Repair-GroupConfiguration" in text
    assert "Runtime attachment will be validated after Start/Smoke." in text
    assert "did not persist the complete Job Queue autoscaling configuration after PATCH" in text


def test_stack_prepare_runs_queue_attachment_repair() -> None:
    text = STACK.read_text(encoding="utf-8")
    assert "repair_salad_queue_attachment.ps1" in text
    assert '$Action -eq "Prepare"' in text
