from pathlib import Path

PREWARM = Path("scripts/start_salad_optimized_prewarm.ps1")


def test_optimized_prewarm_verifies_stale_queue_summary_by_job_enumeration() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "function Get-QueueJobSnapshot" in text
    assert '"pending", "running"' in text
    assert "exhaustive job enumeration found no pending or running jobs" in text
    assert "$VerifiedEmptyQueueLength = Assert-QueueLogicallyEmpty -Queue $Queue" in text
    assert "$ReportedQueueLength -gt $VerifiedEmptyQueueLength" in text
    assert "queue growth from verified-empty baseline" in text


def test_optimized_prewarm_does_not_treat_stale_summary_as_active_work() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    legacy_guard = (
        'if ([int]$Queue.current_queue_length -ne 0) {\n'
        '    throw (\n'
        '        "Optimized prewarm requires an empty queue;'
    )
    assert legacy_guard not in text
