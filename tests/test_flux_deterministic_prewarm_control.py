from pathlib import Path

FLUX_PREWARM = Path("scripts/start_salad_flux_prewarm.ps1")
FLUX_RESTORE = Path("scripts/restore_salad_flux_scale_to_zero.ps1")
PHASE4 = Path("scripts/run_phase4_assets_controlled.ps1")
PHASE6 = Path("scripts/run_phase6_keyframes_controlled.ps1")


def test_flux_prewarm_allocates_one_ready_replica_before_queue_work() -> None:
    script = FLUX_PREWARM.read_text(encoding="utf-8")

    assert "Get-QueueActiveSnapshot" in script
    assert '$Job.status -in @("pending", "running")' in script
    assert "could not exhaustively inspect queue jobs before GPU allocation" in script
    assert "queue summary is stale" in script
    assert "Continuing safely" in script
    assert '@{ replicas = 1 }' in script
    assert '"$GroupUrl/start"' in script
    assert "$Started -and" in script
    assert "$Ready" in script
    assert "min_replicas = 1" in script
    assert "one ready replica held for fallback queue work" in script
    assert "Repair" not in script


def test_flux_restore_returns_manifest_scale_to_zero_and_stops() -> None:
    script = FLUX_RESTORE.read_text(encoding="utf-8")

    assert "min_replicas = [int]$Definition.autoscaler.min_replicas" in script
    assert '"$GroupUrl/stop"' in script
    assert 'Status -eq "stopped"' in script
    assert "replicas -eq 0" in script
    assert "Repair" not in script


def test_phase4_uses_ready_flux_prewarm_instead_of_zero_replica_arm() -> None:
    script = PHASE4.read_text(encoding="utf-8")

    assert '"start_salad_flux_prewarm.ps1"' in script
    assert '"restore_salad_flux_scale_to_zero.ps1"' in script
    assert "prewarm one ready replica before queue submission" in script
    assert "arm_salad_scale_to_zero.ps1" not in script


def test_phase6_uses_cached_or_on_demand_ready_flux_fallback() -> None:
    script = PHASE6.read_text(encoding="utf-8")

    assert '"start_salad_flux_prewarm.ps1"' in script
    assert '"restore_salad_flux_scale_to_zero.ps1"' in script
    assert "--prewarm-fallback-on-demand" in script
    assert "prewarm only because cached safety evidence requires it" in script
    assert '"start_salad_scale_to_zero.ps1"' not in script
