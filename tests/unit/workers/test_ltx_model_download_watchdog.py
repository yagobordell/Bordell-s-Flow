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
    assert "--min-progress-reset-bytes" in script
    assert "--min-throughput-mibps" in script
    assert "--throughput-grace-seconds" in script
    assert "--throughput-window-seconds" in script
    assert "--reallocate-on-slow" in script
    assert "/usr/local/bin/network-preflight" in script


def test_ltx_model_download_preserves_per_file_fast_path() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert 'if [[ -s "${destination}" ]]' in script
    assert "MODEL_PRESENT" in script
    assert "MODEL_DOWNLOAD_START" in script
    assert "MODEL_DOWNLOAD_DONE" in script
    assert "MODEL_READY" in script


def test_ltx_salad_manifest_prefers_fast_high_priority_5090_nodes() -> None:
    import json

    manifest = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = manifest["services"]["ltx25"]

    assert service["priority"] == "high"
    assert service["resources"]["gpu_class_names"] == ["RTX 5090 (32 GB)"]
    assert service["environment"]["SALAD_NETWORK_MIN_DOWNLOAD_MBPS"] == "100"
    assert service["environment"]["SALAD_NETWORK_TEST_ATTEMPTS"] == "3"
    assert service["environment"]["LTX_MODEL_DOWNLOAD_MIN_THROUGHPUT_MIBPS"] == "8"
