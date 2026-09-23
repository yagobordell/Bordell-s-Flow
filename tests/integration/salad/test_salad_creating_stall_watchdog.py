from pathlib import Path

PREWARM = Path("scripts/salad/start_salad_optimized_prewarm.ps1")


def test_creating_without_pull_progress_uses_bounded_prepull_watchdog() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert '$InstanceState -in @("allocating", "creating")' in text
    assert "$PullingProgress -le 0.0" in text
    assert "Allocation/container creation made no image-pull progress" in text


def test_transient_missing_instance_does_not_reset_same_node_watchdogs() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "$ObservedInstance = (" in text
    assert "if ($ObservedInstance) {" in text
    assert "$IdentityChanged = $false" in text
    assert "$MachineId -ne $CurrentMachineId" in text
    assert "$CurrentInstanceId = $InstanceId" in text
    assert "$CurrentMachineId = $MachineId" in text
