from pathlib import Path

BOOTSTRAP = Path("scripts/start_salad_protected_smoke.ps1")
RESTORE = Path("scripts/restore_salad_scale_to_zero.ps1")
MANAGER = Path("scripts/manage_salad_validation.ps1")


def test_bootstrap_uses_manual_replica_with_scale_to_zero_autoscaler() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "@{ replicas = 1 }" in script
    assert "min_replicas = 1" not in script
    assert "New-BootstrapAutoscaler" not in script
    assert "[int]$Group.queue_autoscaler.min_replicas -eq 0" in script
    assert '"$GroupUrl/instances"' in script
    assert "$Instances.Count -eq 1" in script
    assert "$StartedInstances.Count -eq 1" in script
    assert "Test-QueueAttachment" in script
    assert "[ValidateRange(1, 120)]" in script
    assert "[int]$TimeoutMinutes = 90" in script
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
    assert "queue attachment observation=$Attached" in script
    assert "one started ready bootstrap instance before timeout" in script

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


def test_manual_stop_also_restores_scale_to_zero_configuration() -> None:
    manager = MANAGER.read_text(encoding="utf-8")
    safe_stop = manager.split("function Invoke-SafeStop", maxsplit=1)[1].split(
        "switch ($Action)", maxsplit=1
    )[0]

    assert "Invoke-ScaleToZeroRestore" in safe_stop
    assert 'Invoke-StackAction -StackAction "Stop"' in safe_stop


def test_ltx_fractional_download_reallocates_slow_salad_node() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "[int]$MaxDownloadReallocations = 3" in script
    assert 'function Request-InstanceReallocation' in script
    assert '"$InstancesUrl/$InstanceId/reallocate"' in script
    assert '$Service -eq "ltx25"' in script
    assert '$InstanceState -eq "downloading"' in script
    assert "$PullingProgressValue -gt 0.0" in script
    assert "$PullingProgressValue -lt 1.0" in script
    assert "$DownloadReallocations -ge $MaxDownloadReallocations" in script
    assert "$ReallocationPending = $true" in script
    assert "$MachineId -ne $ReallocatedMachineId" in script
    assert "Request-InstanceReallocation -InstanceId $InstanceId" in script
    assert "$StartedBootstrapDeadlineSet = $false" in script
