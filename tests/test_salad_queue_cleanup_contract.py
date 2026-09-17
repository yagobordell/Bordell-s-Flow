from pathlib import Path

CLEANUP = Path("scripts/cleanup_salad_queue.ps1")


def test_queue_cleanup_keeps_paginated_items_array_shaped() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert "$Items = @(" in script
    assert '$Response.PSObject.Properties.Name -contains "items"' in script
    assert '$Response.PSObject.Properties.Name -contains "jobs"' in script
    assert "$Items.Count -lt 25" in script
    assert "$Items = if (" not in script


def test_queue_cleanup_fast_paths_empty_queue_before_history_scan() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    summary = script.index("$Queue = Get-QueueSummary")
    empty_check = script.index("$Queue.current_queue_length -eq 0")
    history_scan = script.index("$ActiveJobs = @(Get-ActiveQueueJobs -Deadline $Deadline)")
    assert summary < empty_check < history_scan
    assert "before historical job pagination" in script


def test_queue_cleanup_bounds_history_scan_by_cleanup_deadline() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert "Get-ActiveQueueJobs -Deadline $Deadline" in script
    assert "if ((Get-Date) -ge $Deadline)" in script
    assert "$RequestTimeoutSeconds" in script
    assert "cleanup deadline in" in script


def test_queue_cleanup_supports_flux_schnell() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert '"flux_schnell"' in script
    assert "Queue cleanup requires" in script
    assert "queue cleanup complete: no active or queued jobs" in script
