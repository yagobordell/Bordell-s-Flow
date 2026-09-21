from pathlib import Path

PHASE5_CONTROLLED = Path("scripts/run_phase5_audio_controlled.ps1")


def test_phase5_breeze_prewarm_budget_allows_bounded_recovery() -> None:
    text = PHASE5_CONTROLLED.read_text(encoding="utf-8")

    assert "[int]$PrewarmTimeoutMinutes = 90" in text
    assert "Invoke-Prewarm -Service breeze_tts2" in text
    assert "TimeoutMinutes = $PrewarmTimeoutMinutes" in text


PHASE5_ALIGNMENT_CONTROLLED = Path("scripts/run_phase5_alignment_controlled.ps1")


def test_phase5_whisper_prewarm_budget_allows_reallocation_recovery() -> None:
    text = PHASE5_ALIGNMENT_CONTROLLED.read_text(encoding="utf-8")

    assert "[int]$PrewarmTimeoutMinutes = 120" in text
    assert 'Service = "whisper"' in text
    assert "TimeoutMinutes = $PrewarmTimeoutMinutes" in text
    assert "HoldReadyReplica = $true" in text
