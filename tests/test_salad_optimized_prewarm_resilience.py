from pathlib import Path


PREWARM = Path("scripts/start_salad_optimized_prewarm.ps1")


def test_optimized_prewarm_retries_transient_control_plane_reads() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "function Invoke-SaladRead" in text
    assert "function Test-TransientSaladReadFailure" in text
    assert "WebExceptionStatus]::Timeout" in text
    assert "$StatusCode -eq 408" in text
    assert "$StatusCode -eq 429" in text
    assert "$StatusCode -ge 500" in text
    assert "without reallocating the worker" in text
    assert 'Get-Group {\n    return Invoke-SaladRead' in text
    assert 'Get-Queue {\n    return Invoke-SaladRead' in text
    assert 'Get-Instances {\n    $Response = Invoke-SaladRead' in text
