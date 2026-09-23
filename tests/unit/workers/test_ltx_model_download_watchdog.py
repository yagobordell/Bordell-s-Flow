from pathlib import Path

BOOTSTRAP = Path("docker/workers/ltx25/download_models.sh")


def test_ltx_model_download_reuses_shared_watchdog() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "ai_video_factory.workers.download_watchdog" in script
    assert 'LTX_MODEL_DOWNLOAD_PROGRESS_INTERVAL_SECONDS:-30' in script
    assert 'LTX_MODEL_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600' in script
    assert 'LTX_MODEL_DOWNLOAD_HARD_TIMEOUT_SECONDS:-21600' in script
    assert "--progress-root" in script
    assert "--stall-timeout-seconds" in script
    assert "--hard-timeout-seconds" in script
    assert "--poll-seconds" in script


def test_ltx_model_download_preserves_per_file_fast_path() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert 'if [[ -s "${destination}" ]]' in script
    assert "MODEL_PRESENT" in script
    assert "MODEL_DOWNLOAD_START" in script
    assert "MODEL_DOWNLOAD_DONE" in script
    assert "MODEL_READY" in script
