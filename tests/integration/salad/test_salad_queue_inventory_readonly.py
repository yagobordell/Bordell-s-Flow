from pathlib import Path

INSPECTOR = Path("scripts/diagnostics/inspect_salad_queue_state.ps1")
CLEANUP = Path("scripts/salad/cleanup_salad_queue.ps1")


def test_queue_inventory_is_read_only_and_uses_documented_paginated_jobs_api() -> None:
    source = INSPECTOR.read_text(encoding="utf-8")
    assert 'Invoke-RestMethod -Method Get' in source
    assert '"$QueueUrl/jobs?page=$Page&page_size=$PageSize"' in source
    assert '$PageSize = 25' in source
    assert '$PageSize = 100' not in source
    assert 'for ($Page = 1; $Page -le 100; $Page++)' in source
    assert '$_ .status' not in source
    assert '$_ .input' not in source
    assert '-Method Patch' not in source
    assert '-Method Delete' not in source
    assert '-Method Post' not in source
    assert '$Job.input' not in source
    assert '$Job.output' not in source
    assert 'Read-only diagnostic complete; Salad resources were not modified.' in source


def test_queue_inventory_reports_summary_listing_discrepancy_without_destructive_fallback() -> None:
    source = INSPECTOR.read_text(encoding="utf-8")
    assert '"queue.length.before=' in source
    assert '"queue.length.after=' in source
    assert '"queue.pagination_complete=' in source
    assert '"queue.jobs_listed=' in source
    assert '"queue.active_jobs_listed=' in source
    assert '"group.autoscaler.min_replicas=' in source
    assert '$Active.Count -eq 0' in source
    assert 'the complete job listing contains no pending/running work' in source
    assert 'no empty-queue conclusion' in source
    # The pre-existing cleanup script may cancel jobs; diagnostic must not call it.
    assert 'cleanup_salad_queue.ps1' not in source
    assert 'Delete Job' not in source


def test_existing_queue_cleanup_still_requires_stopped_zero_replica_guard() -> None:
    source = CLEANUP.read_text(encoding="utf-8")
    assert '$Status -ne "stopped" -or [bool]$Group.pending_change' in source
    assert '[int]$Group.replicas -ne 0' in source
