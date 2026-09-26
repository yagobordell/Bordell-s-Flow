from pathlib import Path

from ai_video_factory.workers.ltx25.model_manifest import (
    validate_installed_model_manifest,
    write_installed_model_manifest,
)

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


def test_ltx_pinned_bundle_download_is_bounded_and_preserves_manifest() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'MAX_DOWNLOAD_WORKERS="${LTX_MODEL_DOWNLOAD_MAX_WORKERS:-1}"' in script
    assert '[[ ! "${MAX_DOWNLOAD_WORKERS}" =~ ^[12]$ ]]' in script
    assert 'hf download "${MODEL_REPOSITORY}" "${MODEL_FILES[@]}"' in script
    assert '--max-workers 2' in script
    assert '--revision "${MODEL_REVISION}"' in script
    assert '--progress-root "${MODEL_ROOT}"' in script
    assert '--reallocate-on-slow' in script
    assert script.index('--max-workers 2') < script.index('ltx25.model_manifest write')
    assert script.index('MODEL_VERIFY_DONE ${model_file}') < script.index(
        'ltx25.model_manifest write'
    )


def test_ltx_model_download_fast_path_requires_pinned_provenance() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert ".bordell-installed-model-manifest.json" in script
    assert "MODEL_MANIFEST_VALID" in script
    assert "manifest_is_valid" in script
    assert "ltx25.model_manifest validate" in script
    assert "ltx25.model_manifest write" in script
    assert "MODEL_VERIFY_START" in script
    assert "MODEL_VERIFY_DONE" in script
    assert "MODEL_READY" in script


def test_ltx_model_manifest_rejects_same_size_content_corruption(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    model = root / "weights" / "model.safetensors"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"abcd")
    installed = root / ".bordell-installed-model-manifest.json"
    relative = "weights/model.safetensors"

    write_installed_model_manifest(
        installed,
        repository="Lightricks/LTX-2.5",
        revision="pinned-revision",
        root=root,
        files=[relative],
    )
    assert validate_installed_model_manifest(
        installed,
        repository="Lightricks/LTX-2.5",
        revision="pinned-revision",
        root=root,
        expected_files=[relative],
    )

    model.write_bytes(b"abce")
    assert model.stat().st_size == 4
    assert not validate_installed_model_manifest(
        installed,
        repository="Lightricks/LTX-2.5",
        revision="pinned-revision",
        root=root,
        expected_files=[relative],
    )


def test_ltx_salad_manifest_prefers_fast_high_priority_5090_nodes() -> None:
    import json

    manifest = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = manifest["services"]["ltx25"]

    assert service["priority"] == "high"
    assert service["resources"]["gpu_class_names"] == ["RTX 5090 (32 GB)"]
    assert service["environment"]["SALAD_NETWORK_MIN_DOWNLOAD_MBPS"] == "100"
    assert service["environment"]["SALAD_NETWORK_TEST_ATTEMPTS"] == "3"
    assert service["environment"]["LTX_MODEL_DOWNLOAD_MIN_THROUGHPUT_MIBPS"] == "8"
    assert "huggingface.co/Lightricks/LTX-2.5/resolve/" in service["environment"][
        "SALAD_NETWORK_TEST_URL"
    ]
    assert service["environment"]["HF_HUB_DOWNLOAD_TIMEOUT"] == "60"
    assert service["environment"]["HF_HUB_ETAG_TIMEOUT"] == "15"
    assert service["environment"]["HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY"] == "true"
    assert "HF_XET_HIGH_PERFORMANCE" not in service["environment"]
    assert service["environment"]["LTX_MODEL_DOWNLOAD_MAX_WORKERS"] == "2"
