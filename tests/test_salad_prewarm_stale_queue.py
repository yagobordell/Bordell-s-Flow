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


def test_optimized_prewarm_rechecks_stale_queue_only_at_safe_boundaries() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "$VerifiedInitialQueueLength" in text
    assert "observed new queued work after the pre-allocation empty-queue verification" in text
    assert "observed queue growth beyond the already-verified stale summary" in text
    assert "refusing GPU allocation while queue state is ambiguous" in text
    assert "enumerable pending/running job(s)" in text
    assert "-VerificationSeconds 180" in text


def test_optimized_prewarm_does_not_treat_stale_summary_as_active_work() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    legacy_guard = (
        'if ([int]$Queue.current_queue_length -ne 0) {\n'
        '    throw (\n'
        '        "Optimized prewarm requires an empty queue;'
    )
    assert legacy_guard not in text


def test_optimized_prewarm_does_not_paginate_stale_history_on_every_poll() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    loop = text.split("while ((Get-Date) -lt $Deadline)", maxsplit=1)[1]
    ready = loop.split("$Queue = Get-Queue", maxsplit=1)[1]
    assert "$null = Assert-QueueLogicallyEmpty -Queue $Queue" in ready
    poll_prefix = loop.split("# The queue is dedicated to this service.", maxsplit=1)[0]
    assert "Assert-QueueLogicallyEmpty -Queue $Queue" not in poll_prefix
