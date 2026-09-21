from pathlib import Path

PREWARM = Path("scripts/start_salad_optimized_prewarm.ps1")


def test_optimized_prewarm_verifies_stale_queue_summary_by_job_enumeration() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "function Get-QueueJobSnapshot" in text
    assert "function Test-PreflightVerifiedQueueEmpty" in text
    assert "AI_VIDEO_FACTORY_PREFLIGHT_QUEUE_EMPTY" in text
    assert "skipping duplicate historical pagination" in text
    assert '"pending", "running"' in text
    assert "exhaustive job enumeration found no pending or running jobs" in text
    assert text.count("$null = Assert-QueueLogicallyEmpty -Queue $Queue") == 4
    assert "$VerifiedInitialQueueLength = [int]$Queue.current_queue_length" in text
    assert "prewarm adopted one already started+ready shared replica" in text


def test_optimized_prewarm_rechecks_summary_growth_without_trusting_stale_count() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "$VerifiedInitialQueueLength" in text
    assert "function Resolve-QueueSummaryGrowth" in text
    assert "$ReportedQueueLength -gt $VerifiedInitialQueueLength" in text
    assert "Verifying enumerable jobs before treating the growth as new active work." in text
    assert "refusing to continue while queue state is ambiguous" in text
    assert "enumerable pending/running job(s); refusing to continue" in text
    assert "Rebasing the verified stale summary." in text
    assert "-VerificationSeconds 60" in text
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

    loop = text.split("$ImagePullProgressThreshold = 0.005", maxsplit=1)[1]
    poll_prefix, ready_suffix = loop.split(
        "# The queue is dedicated to this service.", maxsplit=1
    )
    assert "Assert-QueueLogicallyEmpty -Queue $Queue" not in poll_prefix
    assert "if ($ReportedQueueLength -gt $VerifiedInitialQueueLength)" in poll_prefix
    assert "Resolve-QueueSummaryGrowth" in poll_prefix
    assert "$null = Assert-QueueLogicallyEmpty -Queue $Queue" in ready_suffix



def test_optimized_prewarm_rebases_only_verified_stale_summary_growth() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    helper = text.split("function Resolve-QueueSummaryGrowth", maxsplit=1)[1].split(
        "function Get-Instances", maxsplit=1
    )[0]
    assert "Get-QueueJobSnapshot" in helper
    assert "if (-not [bool]$Snapshot.complete)" in helper
    assert "$ActiveJobs.Count -gt 0" in helper
    assert "return $ReportedLength" in helper
