from pathlib import Path


CLEANUP = Path("scripts/cleanup_salad_queue.ps1")


def test_queue_cleanup_keeps_paginated_items_array_shaped() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert "$Items = @(" in script
    assert '$Response.PSObject.Properties.Name -contains "items"' in script
    assert '$Response.PSObject.Properties.Name -contains "jobs"' in script
    assert "$Items.Count -lt 25" in script
    assert "$Items = if (" not in script


def test_queue_cleanup_supports_flux_schnell() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert '"flux_schnell"' in script
    assert "Queue cleanup requires" in script
    assert "queue cleanup complete: no active or queued jobs" in script
