from pathlib import Path


def test_ideogram_phases_hold_ready_replica_across_prompt_variants() -> None:
    hold = Path("scripts/hold_salad_warm_replica.ps1").read_text(encoding="utf-8")

    for runner_path, phase_runner in (
        (Path("scripts/run_phase4_assets_controlled.ps1"), "run_phase4_assets.py"),
        (Path("scripts/run_phase6_keyframes_controlled.ps1"), "run_phase6_keyframes.py"),
    ):
        runner = runner_path.read_text(encoding="utf-8")
        assert "hold_salad_warm_replica.ps1" in runner
        assert runner.index("start_salad_optimized_prewarm.ps1") < runner.index(
            "hold_salad_warm_replica.ps1"
        )
        assert runner.index("hold_salad_warm_replica.ps1") < runner.index(phase_runner)
        assert "-Action Stop" in runner
        assert "cleanup_salad_queue.ps1" in runner

    assert "min_replicas = 1" in hold
    assert "exactly one already-started ready replica" in hold
    assert "warm replica hold active" in hold
