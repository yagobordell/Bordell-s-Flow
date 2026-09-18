from pathlib import Path

FLUX_PREWARM = Path("scripts/start_salad_flux_prewarm.ps1")
FLUX_RESTORE = Path("scripts/restore_salad_flux_scale_to_zero.ps1")
GENERIC_RESTORE = Path("scripts/restore_salad_scale_to_zero.ps1")
WORKER_MANAGER = Path("scripts/manage_salad_worker.ps1")
ZERO_REPLICA_GUARD = Path("scripts/ensure_salad_zero_replicas.ps1")
QUEUE_CLEANUP = Path("scripts/cleanup_salad_queue.ps1")
STACK_MANAGER = Path("scripts/manage_salad_stack.ps1")
SCALE_TO_ZERO_STARTER = Path("scripts/start_salad_scale_to_zero.ps1")


def test_critical_salad_control_plane_paths_retry_transient_failures() -> None:
    for path in (
        FLUX_PREWARM,
        FLUX_RESTORE,
        GENERIC_RESTORE,
        WORKER_MANAGER,
        ZERO_REPLICA_GUARD,
        QUEUE_CLEANUP,
        SCALE_TO_ZERO_STARTER,
    ):
        text = path.read_text(encoding="utf-8")
        assert "Test-TransientSaladFailure" in text, path.name
        assert "503" in text, path.name
        assert "504" in text, path.name
        assert "upstream connect error" in text, path.name
        assert "Retrying in" in text, path.name


def test_flux_prewarm_retries_reads_and_idempotent_control_writes() -> None:
    text = FLUX_PREWARM.read_text(encoding="utf-8")

    assert 'Operation "read container group"' in text
    assert 'Operation "read container instances"' in text
    assert 'Operation "request one FLUX replica"' in text
    assert 'Operation "start FLUX container group"' in text
    assert 'Operation "hold ready FLUX replica"' in text


def test_stack_stop_runs_zero_replica_guard_after_stop_request_error() -> None:
    text = STACK_MANAGER.read_text(encoding="utf-8")

    assert "$StopFailures = @()" in text
    assert "$StopError = $null" in text
    assert "Salad Stop request failed for '$Name'" in text
    assert "zero-replica guard will still" in text
    assert "verify the terminal state" in text
    assert "Invoke-ZeroReplicaGuard -Name $Name" in text
    assert "treating cleanup as successful" in text
    assert "could not verify terminal zero-replica state" in text


def test_queue_cleanup_retries_control_plane_and_cancels_active_jobs() -> None:
    text = QUEUE_CLEANUP.read_text(encoding="utf-8")

    assert 'Operation "read stopped container group"' in text
    assert 'Operation "read queue summary"' in text
    assert 'Operation "inspect queue jobs page $Page"' in text
    assert 'Operation "cancel active queue job $([string]$Job.id)"' in text


def test_generic_scale_to_zero_restore_retries_transient_control_plane_failures() -> None:
    text = GENERIC_RESTORE.read_text(encoding="utf-8")

    assert 'Operation "read container group"' in text
    assert 'Operation "restore manifest autoscaler"' in text
    assert "Test-TransientSaladFailure" in text


def test_scale_to_zero_starter_routes_reads_and_writes_through_retry_wrapper() -> None:
    text = SCALE_TO_ZERO_STARTER.read_text(encoding="utf-8")

    assert 'Operation "read container group"' in text
    assert 'Operation "read queue summary"' in text
    assert 'Operation "start container group"' in text
    assert 'Operation "request protected bootstrap replica"' in text
    direct_calls = [line for line in text.splitlines() if "Invoke-RestMethod" in line]
    assert direct_calls == ["            return Invoke-RestMethod @Arguments"]
