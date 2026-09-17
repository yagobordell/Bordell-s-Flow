from pathlib import Path

PREWARM = Path("scripts/start_salad_optimized_prewarm.ps1")


def test_optimized_prewarm_has_post_pull_start_watchdog() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "PostPullStartSeconds" in text
    assert "FinalPostPullStartSeconds" in text
    assert "MaxPostPullStartReallocations" in text
    assert "$PostPullStartSince = $null" in text
    assert "$PostPullStartReallocations = 0" in text
    assert '$ContainerObservedRunning = $InstanceState -eq "running"' in text
    assert "$ContainerStarted = $Started -or $ContainerObservedRunning" in text
    assert "$PullingProgress -ge 1.0 -and" in text
    assert "-not $ContainerStarted" in text
    assert "$ContainerStarted -and $InstanceState -eq \"running\" -and -not $Ready" in text
    assert "Image pull completed but the container did not start" in text
    assert "image pull completed but the container never started" in text


def test_optimized_prewarm_accepts_ready_running_instance_when_started_flag_lags() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "$ContainerStarted -and" in text
    assert "$Ready" in text
    assert "$Started -and\n        $Ready" not in text


def test_optimized_prewarm_formats_profile_and_status_lines_before_write_host() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "$ProfileLine = (" in text
    assert "$StatusLine = (" in text
    assert "Write-Host $ProfileLine -ForegroundColor Cyan" in text
    assert "Write-Host $StatusLine" in text
