from pathlib import Path


def test_ideogram_phases_pin_replica_before_readiness() -> None:
    prewarm = Path("scripts/start_salad_optimized_prewarm.ps1").read_text(encoding="utf-8")

    assert "[switch]$HoldReadyReplica" in prewarm
    assert "$TargetMinReplicas = if ($HoldReadyReplica) { 1 } else { 0 }" in prewarm
    assert '$PrewarmPatch["queue_autoscaler"]' in prewarm
    assert "min_replicas = 1" in prewarm
    assert prewarm.index("$PrewarmPatch") < prewarm.index('"$GroupUrl/start"')
    assert "queue_autoscaler.min_replicas -eq $TargetMinReplicas" in prewarm

    for runner_path, phase_runner in (
        (Path("scripts/run_phase4_assets_controlled.ps1"), "run_phase4_assets.py"),
        (Path("scripts/run_phase6_keyframes_controlled.ps1"), "run_phase6_keyframes.py"),
    ):
        runner = runner_path.read_text(encoding="utf-8")
        assert "HoldReadyReplica = $true" in runner
        assert "hold_salad_warm_replica.ps1" not in runner
        assert runner.index("start_salad_optimized_prewarm.ps1") < runner.index(phase_runner)
        assert "-Action Stop" in runner
        assert "cleanup_salad_queue.ps1" in runner
