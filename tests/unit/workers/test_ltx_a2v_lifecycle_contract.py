from __future__ import annotations

from pathlib import Path

SMOKE = Path("scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1")


def test_ltx_smoke_lifecycle_has_separate_phases_and_cold_wall_clock() -> None:
    source = SMOKE.read_text(encoding="utf-8")
    for name in (
        "preflight_seconds",
        "capacity_start_seconds",
        "readiness_seconds",
        "smoke_seconds",
        "cleanup_seconds",
        "end_to_end_seconds",
    ):
        assert name in source
    assert 'LTX25_A2V_LIFECYCLE_METRICS ' in source
    assert 'schema_version = 1' in source
    assert source.index("$LifecycleMetrics.preflight_seconds =") < source.index(
        "& $WorkerManager @Start"
    )
    assert source.index("$LifecycleMetrics.capacity_start_seconds =") < source.index(
        "& $Python $ReadyWait @ReadyArgs"
    )
    assert source.index("$LifecycleMetrics.readiness_seconds =") < source.index(
        "& $Python $Smoke @Arguments"
    )


def test_lifecycle_logging_never_bypasses_readiness_or_cleanup() -> None:
    source = SMOKE.read_text(encoding="utf-8")
    assert "AllowBootstrappingInstance = $true" in source
    assert source.index("& $Python $ReadyWait @ReadyArgs") < source.index(
        "& $Python $Smoke @Arguments"
    )
    assert source.index("& $WorkerManager -Action Stop -Service ltx25") < source.index(
        "LTX25_A2V_LIFECYCLE_METRICS "
    )
    assert 'if ($PhaseName -and $null -eq $LifecycleMetrics[$PhaseName])' in source
    assert 'outcome = "failed"' in source
    assert '$LifecycleMetrics.outcome = "cleanup_failed"' in source
    assert 'stopped/replicas=0/pending=False' in source
    assert "reference-compiled" not in source
