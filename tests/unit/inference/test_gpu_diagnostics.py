from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_factory.inference import gpu_diagnostics as diagnostics
from ai_video_factory.inference.storage import LocalObjectStorage


@pytest.fixture
def diagnostic_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diagnostics,
        "_gpu_identity",
        lambda: {"nvidia_smi": "0, GPU-abc, 0000:01:00.0, RTX 5090, 32768"},
    )
    monkeypatch.setattr(
        diagnostics,
        "_cuda_snapshot",
        lambda: {
            "available": True,
            "device_free_bytes": 16_000_000,
            "memory_reserved_bytes": 8_000_000,
            "num_ooms": 1,
        },
    )
    monkeypatch.setattr(
        diagnostics, "_read_linux_memory", lambda: {"vmrss_kb": 100_000}
    )


def _journal(tmp_path: Path, storage: LocalObjectStorage) -> diagnostics.GpuJobDiagnostics:
    return diagnostics.GpuJobDiagnostics(
        storage=storage,
        work_dir=tmp_path / "work",
        job_id="ltx-a2v-diagnostic-test",
        attempt_count=2,
        worker_id="test-worker",
        instance_id="test-instance",
        generation_profile="diagnostic-v4",
    )


def test_a2v_diagnostics_persist_phase_snapshots_and_full_failure(
    tmp_path: Path, diagnostic_dependencies: None
) -> None:
    del diagnostic_dependencies
    storage = LocalObjectStorage(tmp_path / "objects")
    journal = _journal(tmp_path, storage)
    with diagnostics.gpu_diagnostics_scope(journal):
        diagnostics.diagnostic_checkpoint("reference.gemma.prompt_encode.start")
        try:
            raise RuntimeError("CUDA error: out of memory")
        except RuntimeError as error:
            journal.failure(error)
            assert error._gpu_diagnostic_phase == "reference.gemma.prompt_encode.start"
            assert error._gpu_diagnostic_key == journal.last_key
    assert diagnostics._ACTIVE_DIAGNOSTICS.get() is None
    prefix = "diagnostics/ltx-a2v/ltx-a2v-diagnostic-test/attempt-02/"
    first = storage._path(prefix + "001-reference-gemma-prompt_encode-start.json")
    second = storage._path(prefix + "002-failed.json")
    assert first.is_file()
    assert second.is_file()
    snapshot = json.loads(first.read_text(encoding="utf-8"))
    failed = json.loads(second.read_text(encoding="utf-8"))
    assert snapshot["gpu_identity"]["nvidia_smi"].startswith("0, GPU-abc")
    assert snapshot["cuda"]["device_free_bytes"] == 16_000_000
    assert snapshot["host_memory"]["vmrss_kb"] == 100_000
    assert failed["failure"]["failed_during_phase"] == "reference.gemma.prompt_encode.start"
    assert "RuntimeError: CUDA error: out of memory" in failed["failure"]["traceback"]
    assert "diagnostic-v4" == failed["generation_profile"]


def test_r2_failure_does_not_replace_original_gpu_error(
    tmp_path: Path, diagnostic_dependencies: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del diagnostic_dependencies
    storage = LocalObjectStorage(tmp_path / "objects")
    journal = _journal(tmp_path, storage)

    def fail_upload(*args, **kwargs):
        del args, kwargs
        raise ConnectionError("R2 unavailable")

    monkeypatch.setattr(storage, "create_if_absent", fail_upload)
    journal.checkpoint("reference.gemma.prompt_encode.start")
    try:
        raise RuntimeError("CUDA error: out of memory")
    except RuntimeError as error:
        journal.failure(error)
        assert error._gpu_diagnostic_phase == "reference.gemma.prompt_encode.start"
        assert error._gpu_diagnostic_key is None
        assert "CUDA error: out of memory" in str(error)
