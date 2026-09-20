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


def test_optimized_prewarm_routes_all_critical_control_plane_calls_through_retry_helpers() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert 'Operation "queue jobs page $Page"' in text
    assert 'Operation "persist one-replica prewarm state"' in text
    assert 'Operation "start prewarmed container group"' in text
    assert 'Operation "reallocate instance $InstanceId"' in text
    assert "-MaxAttempts 6" in text
    assert "upstream connect error" in text
    assert "disconnect/reset" in text

    direct_calls = [
        line
        for line in text.splitlines()
        if "Invoke-RestMethod" in line
    ]
    assert direct_calls == [
        "            return Invoke-RestMethod `",
        "            return Invoke-RestMethod @Arguments",
    ]


def test_reallocation_verifies_instance_state_before_retrying_lost_response() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    block = text.split("function Request-InstanceReallocation", maxsplit=1)[1]
    block = block.split("function Test-QueueAttachment", maxsplit=1)[0]
    assert "-MachineId $MachineId" in text
    assert "$Matching.Count -eq 0" in block
    assert "$ObservedMachine -ne $MachineId" in block
    assert "treating the request as accepted" in block
    assert "-MaxAttempts 1" in block


def test_all_reallocation_paths_pass_machine_identity_for_lost_response_verification() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert text.count("Request-InstanceReallocation `") == 5
    assert text.count("-MachineId $MachineId `") == 5


def test_stale_queue_retry_budget_uses_absolute_deadline() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "[datetime]$Deadline = [datetime]::MaxValue" in text
    assert "exceeded its deadline" in text
    assert "-Deadline $Deadline" in text
    assert "$EffectiveTimeoutSeconds" in text


def test_optimized_prewarm_defers_runtime_transport_proof_to_real_job() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    success_block = text.split("$AutoscalerStateReady = (", maxsplit=2)[-1]
    success_block = success_block.split(
        "# The queue is dedicated to this service.",
        maxsplit=1,
    )[0]
    assert "$Ready" in success_block
    assert "$TransportReady" not in success_block
    assert "function Test-QueueTransportHeartbeat" not in text
    assert 'log contains "received heartbeat"' not in text
    assert "Assert-QueueTransportLoggingReady" not in text
    assert "first real job will prove transport" in text

    adopt_block = text.split("$AdoptReadyReplica -and", maxsplit=1)[1]
    adopt_block = adopt_block.split(
        "Optimized prewarm requires '$GroupName' at replicas=0/pending=False",
        maxsplit=1,
    )[0]
    assert "Test-QueueRuntimeReady" not in adopt_block
    assert "queue config verified and still empty" in adopt_block


def test_optimized_prewarm_accepts_active_scale_to_zero_group_without_restarting() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert '$AllowedInitialStatuses = @("stopped", "running")' in text
    assert '$AllowedInitialStatuses += "deploying"' in text
    assert "$AutostartEnabled" in text
    assert '$GroupAlreadyActive = $Status -in @("running", "deploying")' in text
    assert "if (-not $GroupAlreadyActive)" in text
    assert 'Operation "start prewarmed container group"' in text


def test_optimized_prewarm_does_not_depend_on_debug_transport_logs() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "function Assert-QueueTransportLoggingReady" not in text
    assert "function Test-QueueTransportHeartbeat" not in text
    assert 'log contains "received heartbeat"' not in text
    assert "requires remote SALAD_LOG_LEVEL=debug" not in text
    assert "first real job will prove transport" in text


def test_active_prewarm_survives_transient_control_plane_outage() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    assert "control-plane telemetry remained unavailable after bounded read retries" in text
    assert "keeping the current replica untouched" in text
    assert "$SlowImagePullSince = $null" in text
    assert "$RunningNotReadySince = $null" in text


def test_breeze_reallocates_sustained_slow_image_pull() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    breeze = text.split("breeze_tts2 = @{" , maxsplit=1)[1].split("fish_speech = @{" , maxsplit=1)[0]
    assert "SlowImagePullWindowSeconds = 300" in breeze
    assert "SlowImagePullMinProgress = 0.05" in breeze
    assert "image pull remained below minimum sustained progress" in text
    assert "Container image pull advanced only" in text
