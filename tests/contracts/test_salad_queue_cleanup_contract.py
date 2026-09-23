from pathlib import Path

CLEANUP = Path("scripts/salad/cleanup_salad_queue.ps1")


def test_queue_cleanup_keeps_paginated_items_array_shaped() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert "$Items = @(" in script
    assert '$Response.PSObject.Properties.Name -contains "items"' in script
    assert '$Response.PSObject.Properties.Name -contains "jobs"' in script
    assert "$Items.Count -lt 25" in script
    assert "$Items = if (" not in script



def test_queue_cleanup_supports_qwen_image_21() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert '"qwen_image_21"' in script
    assert "Queue cleanup requires" in script
    assert "queue cleanup complete: no active or queued jobs" in script

def test_queue_cleanup_fast_paths_empty_summary_before_job_history() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    summary_index = script.index("$Queue = Get-QueueSummary")
    empty_index = script.index("current_queue_length -eq 0")
    snapshot_index = script.index("Get-QueueJobSnapshot -Deadline $Deadline")

    assert summary_index < empty_index < snapshot_index


def test_queue_cleanup_bounds_job_history_by_global_deadline() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert "param([Parameter(Mandatory)][datetime]$Deadline)" in script
    assert "if ((Get-Date) -ge $Deadline)" in script
    assert "$RequestTimeoutSeconds" in script
    assert "cleanup deadline in" in script


def test_queue_cleanup_accepts_stale_summary_only_after_exhaustive_terminal_scan() -> None:
    script = CLEANUP.read_text(encoding="utf-8")

    assert "Get-QueueJobSnapshot" in script
    assert "active_jobs = @($Active)" in script
    assert "complete = $Complete" in script
    assert "-not [bool]$Snapshot.complete" in script
    assert '$Status -in @("pending", "running")' in script
    assert "queue summary is stale" in script
    assert "Treating the stopped queue as logically empty" in script
