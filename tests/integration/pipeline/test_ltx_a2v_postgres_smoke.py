from pathlib import Path


def test_ltx_a2v_smoke_uses_postgres_transport() -> None:
    script = Path("scripts/smoke/submit_ltx25_a2v_smoke.py").read_text(encoding="utf-8")

    assert "PostgresJobQueueClient" in script
    assert "InferenceJobExecutor" in script
    assert "SaladJobQueueClient" not in script
    assert "/queues/" not in script
    assert "SALAD_LTX25_QUEUE_NAME" not in script
    assert "LTX_A2V_TASK" in script
    assert "DEFAULT_GPU_MAX_ATTEMPTS" in script
    assert "max_attempts=DEFAULT_GPU_MAX_ATTEMPTS" in script
    assert "max_attempts=1" not in script


def test_ltx_a2v_controlled_wrapper_owns_explicit_capacity() -> None:
    script = Path("scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1").read_text(
        encoding="utf-8"
    )

    assert "manage_salad_worker.ps1" in script
    assert 'Action = "Start"' in script
    assert 'Service = "ltx25"' in script
    assert "Replicas = 1" in script
    assert "-Action Stop -Service ltx25" in script
    assert "finally {" in script
    assert "start_salad_protected_smoke.ps1" not in script
