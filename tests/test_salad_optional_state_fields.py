from pathlib import Path

WORKER_MANAGER = Path("scripts/manage_salad_worker.ps1")


def test_worker_manager_tolerates_missing_current_state_fields() -> None:
    text = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "function Get-GroupStatus" in text
    assert "function Get-GroupDescription" in text
    assert '$Group.PSObject.Properties["current_state"]' in text
    assert '$CurrentStateProperty.Value.PSObject.Properties["status"]' in text
    assert '$CurrentStateProperty.Value.PSObject.Properties["description"]' in text
    assert "(Get-GroupDescription -Group $Group)" in text
    assert '@{Name = "Status"; Expression = {Get-GroupStatus -Group $_}}' in text
    assert (
        '@{Name = "Description"; Expression = {Get-GroupDescription -Group $_}}' in text
    )
    assert "$Group.current_state.description" not in text
    assert "$_.current_state.description" not in text
