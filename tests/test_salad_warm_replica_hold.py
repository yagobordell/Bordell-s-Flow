from pathlib import Path


def test_ideogram_phases_pin_replica_before_readiness() -> None:
    prewarm = Path("scripts/start_salad_optimized_prewarm.ps1").read_text(encoding="utf-8")

    assert "[switch]$HoldReadyReplica" in prewarm
    assert "$TargetMinReplicas = if ($HoldReadyReplica) { 1 } else { 0 }" in prewarm
    assert '$PrewarmPatch["queue_autoscaler"]' in prewarm
    assert "min_replicas = 1" in prewarm
    assert "max_replicas = 1" in prewarm
    assert "Test-RemoteAutoscalerBounds" in prewarm
    assert "-ExpectedMaxReplicas 1" in prewarm
    main_prewarm = prewarm.split("$PrewarmPatch = @{" , maxsplit=1)[1]
    assert main_prewarm.index('$PrewarmPatch["queue_autoscaler"]') < main_prewarm.index(
        '"$GroupUrl/start"'
    )
    assert "Test-RemoteAutoscalerMinReplicas" in prewarm
    assert "-ExpectedMinReplicas $TargetMinReplicas" in prewarm
    assert "Optimized prewarm cannot use -HoldReadyReplica because Salad did not expose" in prewarm
    assert '$Group.PSObject.Properties["queue_autoscaler"]' in prewarm
    assert "$Group.queue_autoscaler" not in prewarm

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


def test_phase8_holds_ready_ltx_replica_through_dispatch() -> None:
    runner = Path("scripts/run_phase8_videos_controlled.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Service = "ltx25"' in runner
    assert "HoldReadyReplica = $true" in runner
    assert "DispatchTimeoutSeconds = 300" in runner
    prewarm_block = runner.split("$PrewarmArguments = @{", maxsplit=1)[1]
    assert prewarm_block.index("HoldReadyReplica = $true") < prewarm_block.index(
        "& $OptimizedPrewarm @PrewarmArguments"
    )
    assert "--submit-only" in runner
    assert 'Mode = "WarmScaleOut"' in runner
    submit_index = runner.index("--submit-only")
    scaleout_index = runner.index('Mode = "WarmScaleOut"')
    final_wait_index = runner.rindex("--dispatch-timeout-seconds")
    assert submit_index < scaleout_index < final_wait_index
    assert "-Action Stop" in runner


def test_warm_scaleout_control_preserves_one_replica_floor() -> None:
    control = Path("scripts/restore_salad_scale_to_zero.ps1").read_text(
        encoding="utf-8"
    )

    assert '[ValidateSet("Manifest", "WarmScaleOut")]' in control
    assert 'if ($Mode -eq "WarmScaleOut")' in control
    assert 'throw "WarmScaleOut is currently reserved for the ltx25 production lifecycle."' in control
    assert "min_replicas = $MinReplicas" in control
    assert "max_replicas = [int]$Definition.autoscaler.max_replicas" in control
    assert '"warm scale-out autoscaler armed"' in control
