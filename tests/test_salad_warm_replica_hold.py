from pathlib import Path


def test_phase4_holds_ready_replica_across_prompt_variants() -> None:
    runner = Path("scripts/run_phase4_assets_controlled.ps1").read_text(encoding="utf-8")
    hold = Path("scripts/hold_salad_warm_replica.ps1").read_text(encoding="utf-8")

    assert "hold_salad_warm_replica.ps1" in runner
    assert runner.index("start_salad_optimized_prewarm.ps1") < runner.index(
        "hold_salad_warm_replica.ps1"
    )
    assert runner.index("hold_salad_warm_replica.ps1") < runner.index("run_phase4_assets.py")
    assert "min_replicas = 1" in hold
    assert "exactly one already-started ready replica" in hold
    assert "warm replica hold active" in hold
    assert "-Action Stop" in runner
