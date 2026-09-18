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


def test_optimized_prewarm_repairs_only_residual_warm_hold_autoscaler() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "function Repair-ResidualHeldAutoscaler" in text
    assert "Test-RemoteAutoscalerMatchesManifestExceptMinReplicas" in text
    assert "residual warm-hold autoscaler" in text
    assert 'Operation "restore residual warm-hold autoscaler"' in text
    assert "only a residual warm-hold min_replicas override can be repaired automatically" in text
    assert "requires remote autoscaler min_replicas=0 after normalization" in text
