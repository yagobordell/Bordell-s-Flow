from pathlib import Path

PHASE4 = Path("scripts/pipeline/run_phase4_assets_controlled.ps1")
PHASE6 = Path("scripts/pipeline/run_phase6_keyframes_controlled.ps1")


def test_controlled_image_runners_use_qwen_optimized_prewarm() -> None:
    for path in (PHASE4, PHASE6):
        text = path.read_text(encoding="utf-8")
        assert '"../salad/start_salad_optimized_prewarm.ps1"' in text
        assert 'Service = "qwen_image_21"' in text
        assert '"arm_salad_scale_to_zero.ps1"' not in text


def test_phase4_plan_flattens_json_array_before_counting_hits() -> None:
    text = PHASE4.read_text(encoding="utf-8")

    assert "ConvertFrom-Json |" in text
    assert "ForEach-Object { $_ }" in text
    assert '@($Plan | Where-Object { $_.status -eq "hit" }).Count' in text
