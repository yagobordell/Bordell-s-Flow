from pathlib import Path

ARM = Path("scripts/arm_salad_scale_to_zero.ps1")
PHASE4 = Path("scripts/run_phase4_assets_controlled.ps1")
PHASE6 = Path("scripts/run_phase6_keyframes_controlled.ps1")


def test_scale_to_zero_arm_accepts_deploying_without_waiting_for_running() -> None:
    text = ARM.read_text(encoding="utf-8")

    assert '$Status -in @("deploying", "running")' in text
    assert "scale-to-zero armed" in text
    assert "queue work may now autoscale it" in text
    assert "Wait-ForGroupStatus" not in text


def test_controlled_image_runners_use_deterministic_flux_prewarm() -> None:
    for path in (PHASE4, PHASE6):
        text = path.read_text(encoding="utf-8")
        assert '"start_salad_flux_prewarm.ps1"' in text
        assert '"restore_salad_flux_scale_to_zero.ps1"' in text
        assert '"arm_salad_scale_to_zero.ps1"' not in text
        assert "-Action Start -Service flux_schnell" not in text


def test_phase4_plan_flattens_json_array_before_counting_hits() -> None:
    text = PHASE4.read_text(encoding="utf-8")

    assert "ConvertFrom-Json |" in text
    assert "ForEach-Object { $_ }" in text
    assert '@($Plan | Where-Object { $_.status -eq "hit" }).Count' in text
