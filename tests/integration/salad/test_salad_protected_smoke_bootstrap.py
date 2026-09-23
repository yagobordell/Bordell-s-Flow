from pathlib import Path

BOOTSTRAP = Path("scripts/salad/start_salad_protected_smoke.ps1")
RESTORE = Path("scripts/salad/restore_salad_scale_to_zero.ps1")
MANAGER = Path("scripts/salad/manage_salad_validation.ps1")


def test_bootstrap_uses_manual_replica_with_scale_to_zero_autoscaler() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "@{ replicas = 1 }" in script
    assert "min_replicas = 1" not in script
    assert "New-BootstrapAutoscaler" not in script
    assert '$Group.PSObject.Properties["queue_autoscaler"]' in script
    assert "Test-RemoteAutoscalerMinReplicas" in script
    assert "-ExpectedMinReplicas 0" in script
    assert "$Group.queue_autoscaler" not in script
    assert '"$GroupUrl/instances"' in script
    assert "$Instances.Count -eq 1" in script
    assert "$StartedInstances.Count -eq 1" in script
    assert "Test-QueueAttachment" in script
    assert "[ValidateRange(1, 120)]" in script
    assert "[int]$TimeoutMinutes = 90" in script
    assert '"qwen_image_21"' in script
    assert "$StartedBootstrapDeadlineSet = $false" in script
    assert "-not $StartedBootstrapDeadlineSet -and $StartedInstances.Count -eq 1" in script
    assert "$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)" in script
    assert "$StartedBootstrapDeadlineSet = $true" in script
    assert "started bootstrap timeout window={2}m" in script
    assert '$Instance.PSObject.Properties.Name -contains "state"' in script
    assert '$Instance.PSObject.Properties.Name -contains "pulling_progress"' in script
    assert '$Instance.PSObject.Properties.Name -contains "ready"' in script
    assert "$Message = (" in script
    assert "Write-Host $Message" in script


def test_bootstrap_gates_on_readiness_not_queue_attachment() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    readiness_gate = (
        "$StartedInstances.Count -eq 1 -and\n"
        "        $Ready\n"
        "    ) {"
    )
    attachment_gate = (
        "$StartedInstances.Count -eq 1 -and\n"
        "        $Attached\n"
        "    ) {"
    )

    assert readiness_gate in script
    assert attachment_gate not in script
    assert "initial queue attachment observation=$Attached" in script
    assert "one started ready bootstrap instance before timeout" in script
    assert script.count("$Queue = Get-Queue") == 1
    assert "$InitialQueueAttachment = Test-QueueAttachment -Queue $Queue" in script

def test_restore_returns_autoscaler_to_manifest_before_stop() -> None:
    restore = RESTORE.read_text(encoding="utf-8")
    manager = MANAGER.read_text(encoding="utf-8")

    assert "New-ManifestAutoscaler" in restore
    assert "Test-ManifestAutoscaler" in restore
    assert "queue_autoscaler = New-ManifestAutoscaler" in restore

    protected = manager.split("function Invoke-ProtectedSmoke", maxsplit=1)[1].split(
        "function Invoke-SafeStop", maxsplit=1
    )[0]
    assert protected.index("Invoke-ScaleToZeroRestore") < protected.index(
        'Invoke-StackAction -StackAction "Stop"'
    )


def test_public_prewarm_always_cleans_up_gpu_replica() -> None:
    manager = MANAGER.read_text(encoding="utf-8")
    safe_prewarm = manager.split("function Invoke-SafePrewarm", maxsplit=1)[1].split(
        "if ($Recreate", maxsplit=1
    )[0]

    assert "try {" in safe_prewarm
    assert "Invoke-ProtectedSmokeBootstrap" in safe_prewarm
    assert "finally {" in safe_prewarm
    assert "Invoke-SafeStop" in safe_prewarm
    assert "stopped/replicas=0" in safe_prewarm


def test_manual_stop_also_restores_scale_to_zero_configuration() -> None:
    manager = MANAGER.read_text(encoding="utf-8")
    safe_stop = manager.split("function Invoke-SafeStop", maxsplit=1)[1].split(
        "switch ($Action)", maxsplit=1
    )[0]

    assert "Invoke-ScaleToZeroRestore" in safe_stop
    assert 'Invoke-StackAction -StackAction "Stop"' in safe_stop


def test_ltx_image_pull_only_reallocates_after_progress_stalls() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "[int]$MaxDownloadReallocations = 3" in script
    assert "[int]$DownloadStallTimeoutMinutes = 10" in script
    assert "$DownloadProgressThreshold = 0.005" in script
    assert "$DownloadProgressBaseline = $null" in script
    assert "$DownloadProgressSince = $null" in script
    assert "$DownloadProgressInstanceId = \"\"" in script
    assert 'function Request-InstanceReallocation' in script
    assert '"$InstancesUrl/$InstanceId/reallocate"' in script
    assert '$Service -eq "ltx25"' in script
    assert '$InstanceState -eq "downloading"' in script
    assert "$PullingProgressValue -gt 0.0" in script
    assert "$PullingProgressValue -lt 1.0" in script
    assert (
        "$PullingProgressValue -ge (\n"
        "                $DownloadProgressBaseline + $DownloadProgressThreshold"
    ) in script
    assert "$DownloadProgressSince = Get-Date" in script
    assert (
        "$DownloadStallElapsed.TotalMinutes -ge $DownloadStallTimeoutMinutes"
    ) in script
    assert "$DownloadReallocations -ge $MaxDownloadReallocations" in script
    assert "image-pull watchdog started" in script
    assert "image pull made less than" in script
    assert "$ReallocationPending = $true" in script
    assert "$MachineId -ne $ReallocatedMachineId" in script
    assert "Request-InstanceReallocation -InstanceId $InstanceId" in script
    assert "$StartedBootstrapDeadlineSet = $false" in script


def test_ltx_allocating_watchdog_aborts_stalled_bootstrap() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "[int]$AllocatingTimeoutMinutes = 10" in script
    assert "$AllocatingSince = $null" in script
    assert "$AllocatingInstanceId = \"\"" in script
    assert '$InstanceState -eq "allocating"' in script
    assert "$InstanceId -ne $AllocatingInstanceId" in script
    assert "$AllocatingSince = Get-Date" in script
    assert "$AllocatingElapsed = (Get-Date) - $AllocatingSince" in script
    assert "$AllocatingElapsed.TotalMinutes -ge $AllocatingTimeoutMinutes" in script
    assert "aborting protected bootstrap" in script


def test_ltx_running_not_ready_reallocates_stalled_model_bootstrap() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "[int]$RunningNotReadyTimeoutMinutes = 20" in script
    assert "[int]$MaxRunningNotReadyReallocations = 2" in script
    assert "$RunningNotReadySince = $null" in script
    assert "$RunningNotReadyInstanceId = \"\"" in script
    assert "$RunningNotReadyReallocations = 0" in script
    assert '$InstanceState -eq "running"' in script
    assert "$StartedInstances.Count -eq 1" in script
    assert "-not $Ready" in script
    assert (
        "$RunningNotReadyElapsed.TotalMinutes -ge "
        "$RunningNotReadyTimeoutMinutes"
    ) in script
    assert "-not $ReallocationPending" in script
    assert (
        "$RunningNotReadyReallocations -ge "
        "$MaxRunningNotReadyReallocations"
    ) in script
    assert "running-not-ready watchdog started" in script
    assert "remained running but not ready" in script
    assert "Request-InstanceReallocation -InstanceId $InstanceId" in script



def test_bootstrap_uses_exhaustive_active_jobs_not_stale_queue_summary() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "function Get-ActiveQueueJobs" in script
    assert '"$QueueUrl/jobs?page=$Page&page_size=25"' in script
    assert '$Job.status -in @("pending", "running")' in script
    assert "$Items.Count -lt 25" in script
    assert "could not exhaustively enumerate" in script
    assert "requires no pending/running jobs before bootstrap" in script
    assert "Protected smoke queue summary is stale" in script
    assert "Treating '$QueueName' as logically empty." in script
    assert "contains $([int]$Queue.current_queue_length) job(s)" not in script
