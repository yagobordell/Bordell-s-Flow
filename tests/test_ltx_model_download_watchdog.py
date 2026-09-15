from pathlib import Path

BOOTSTRAP = Path("docker/workers/ltx25/download_models.sh")


def test_ltx_model_download_emits_partial_byte_progress() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert 'LTX_MODEL_DOWNLOAD_PROGRESS_INTERVAL_SECONDS:-30' in script
    assert 'LTX_MODEL_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600' in script
    assert "observed_download_bytes" in script
    assert 'root / ".cache" / "huggingface" / "download"' in script
    assert 'partial_dir.glob("*.incomplete")' in script
    assert "MODEL_DOWNLOAD_PROGRESS" in script
    assert "observed_bytes=" in script
    assert "delta_bytes=" in script
    assert "idle_seconds=" in script


def test_ltx_model_download_aborts_after_no_byte_progress() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "idle_seconds >= DOWNLOAD_STALL_TIMEOUT_SECONDS" in script
    assert "MODEL_DOWNLOAD_STALLED" in script
    assert 'kill -TERM "${download_pid}"' in script
    assert 'kill -KILL "${download_pid}"' in script
    assert "return 124" in script
    assert "MODEL_DOWNLOAD_FAILED" in script


def test_ltx_model_download_preserves_completed_file_fast_path() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert 'if [[ -s "${destination}" ]]' in script
    assert "MODEL_PRESENT" in script
    assert "MODEL_DOWNLOAD_DONE" in script
