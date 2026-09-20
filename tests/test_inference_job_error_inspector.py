from pathlib import Path

SCRIPT = Path("scripts/inspect_inference_job_error.py")


def test_inference_job_error_inspector_is_read_only_and_redacts_dsn() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "SELECT" in text
    assert "FROM gpu.jobs" in text
    assert "transport_job_id = %s" in text
    assert "job_id = %s" in text
    assert "last_error" in text
    assert "attempt_count" in text
    assert "INSERT " not in text
    assert "UPDATE " not in text
    assert "DELETE " not in text
    assert "print(dsn" not in text
