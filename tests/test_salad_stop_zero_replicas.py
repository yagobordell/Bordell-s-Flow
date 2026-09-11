from pathlib import Path

STACK = Path("scripts/manage_salad_stack.ps1")
GUARD = Path("scripts/ensure_salad_zero_replicas.ps1")


def test_stack_stop_runs_zero_replica_guard_after_worker_stop() -> None:
    script = STACK.read_text(encoding="utf-8")
    stop_block = script.split('"Stop" {', maxsplit=1)[1].split("default {", maxsplit=1)[0]

    assert "$ZeroReplicaGuard" in script
    assert "ensure_salad_zero_replicas.ps1" in script
    assert 'Invoke-WorkerAction -Name $Name -WorkerAction "Stop"' in stop_block
    assert "Invoke-ZeroReplicaGuard -Name $Name" in stop_block
    assert stop_block.index('-WorkerAction "Stop"') < stop_block.index("Invoke-ZeroReplicaGuard")


def test_zero_replica_guard_requires_stopped_and_patches_to_zero() -> None:
    script = GUARD.read_text(encoding="utf-8")

    assert 'if ($Status -ne "stopped")' in script
    assert "@{ replicas = 0 }" in script
    assert "-Method Patch" in script
    assert 'if ($Status -eq "stopped" -and $Replicas -eq 0' in script
    assert "did not settle at stopped/replicas=0 before timeout" in script
