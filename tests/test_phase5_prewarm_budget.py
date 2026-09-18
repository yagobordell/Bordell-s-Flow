from pathlib import Path

PHASE5_CONTROLLED = Path("scripts/run_phase5_audio_controlled.ps1")


def test_phase5_breeze_prewarm_budget_allows_bounded_recovery() -> None:
    text = PHASE5_CONTROLLED.read_text(encoding="utf-8")

    assert "[int]$PrewarmTimeoutMinutes = 90" in text
    assert "Invoke-Prewarm -Service breeze_tts2" in text
    assert "TimeoutMinutes = $PrewarmTimeoutMinutes" in text
