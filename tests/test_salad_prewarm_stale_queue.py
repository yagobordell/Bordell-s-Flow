from pathlib import Path

PREWARM = Path("scripts/start_salad_optimized_prewarm.ps1")


def test_optimized_prewarm_verifies_stale_queue_summary_by_job_enumeration() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "function Get-QueueJobSnapshot" in text
    assert '"pending", "running"' in text
    assert "exhaustive job enumeration found no pending or running jobs" in text
    assert "$null = Assert-QueueLogicallyEmpty -Queue $Queue" in text
    assert "if ($ReportedQueueLength -ne 0)" in text
    assert "$null = Assert-QueueLogicallyEmpty -Queue $Queue" in text


def test_optimized_prewarm_reverifies_positive_summary_instead_of_using_growth_baseline() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "$VerifiedEmptyQueueLength" not in text
    assert "queue growth from verified-empty baseline" not in text
    assert "refusing GPU allocation while queue state is ambiguous" in text
    assert "enumerable pending/running job(s)" in text


def test_optimized_prewarm_does_not_treat_stale_summary_as_active_work() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    legacy_guard = (
        'if ([int]$Queue.current_queue_length -ne 0) {\n'
        '    throw (\n'
        '        "Optimized prewarm requires an empty queue;'
    )
    assert legacy_guard not in text
