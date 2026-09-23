from pathlib import Path

STACK = Path("scripts/salad/manage_salad_stack.ps1")
VALIDATION = Path("scripts/salad/manage_salad_validation.ps1")
GUARD = Path("scripts/salad/ensure_salad_zero_replicas.ps1")


def test_stack_stop_runs_zero_replica_guard_after_worker_stop() -> None:
    script = STACK.read_text(encoding="utf-8")
    stop_block = script.split('"Stop" {', maxsplit=1)[1].split("default {", maxsplit=1)[0]

    assert "$ZeroReplicaGuard" in script
    assert "ensure_salad_zero_replicas.ps1" in script
    assert 'Invoke-WorkerAction -Name $Name -WorkerAction "Stop"' in stop_block
    assert "Invoke-ZeroReplicaGuard -Name $Name" in stop_block
    assert stop_block.index('-WorkerAction "Stop"') < stop_block.index("Invoke-ZeroReplicaGuard")


def test_validation_stop_has_zero_replica_fallback_when_stack_stop_errors() -> None:
    script = VALIDATION.read_text(encoding="utf-8")

    assert '$ZeroReplicaGuard = Join-Path $PSScriptRoot "ensure_salad_zero_replicas.ps1"' in script
    assert "function Invoke-ZeroReplicaFallback" in script
    fallback = script.split("function Invoke-ZeroReplicaFallback", maxsplit=1)[1]
    fallback = fallback.split("function Assert-Docker", maxsplit=1)[0]
    assert "Invoke-ZeroReplicaGuard" in fallback
    assert "terminal stopped/replicas=0 state" in fallback
    assert "zero-replica guard verified stopped/replicas=0" in fallback

    safe_stop = script.split("function Invoke-SafeStop", maxsplit=1)[1]
    safe_stop = safe_stop.split("switch ($Action)", maxsplit=1)[0]
    assert 'Invoke-StackAction -StackAction "Stop"' in safe_stop
    assert "Invoke-ZeroReplicaFallback -StopFailure $_" in safe_stop


def test_zero_replica_guard_waits_for_stopped_then_patches_to_zero() -> None:
    script = GUARD.read_text(encoding="utf-8")

    assert "function Wait-ForStoppedGroup" in script
    assert '$Status -eq "stopped" -and -not $Pending' in script
    assert "did not reach stopped state before timeout" in script
    assert "$Group = Wait-ForStoppedGroup -InitialGroup $Group" in script
    assert "@{ replicas = 0 }" in script
    assert '-Method "Patch"' in script
    assert 'Operation "normalize replicas to zero"' in script
    assert 'if ($Status -eq "stopped" -and $Replicas -eq 0' in script
    assert "did not settle at stopped/replicas=0 before timeout" in script


def test_global_cleanup_rechecks_zero_replicas_before_queue_cleanup() -> None:
    script = Path("scripts/pipeline/run_video_factory.ps1").read_text(encoding="utf-8")

    assert (
        '$ZeroReplicaGuard = Join-Path $PSScriptRoot "../salad/ensure_salad_zero_replicas.ps1"'
        in script
    )
    cleanup = script.split("function Invoke-FinalCleanup", maxsplit=1)[1].split(
        "Import-EnvFile -Path $EnvFile", maxsplit=1
    )[0]
    assert "& $ZeroReplicaGuard `" in cleanup
    assert "& $QueueCleanup `" in cleanup
    assert cleanup.index("& $ZeroReplicaGuard `") < cleanup.index("& $QueueCleanup `")
