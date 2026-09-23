from pathlib import Path
import json

NETWORK_PREFLIGHT = Path("docker/workers/common/network_preflight.sh")
MANIFEST = Path("deploy/salad/services.json")


def test_network_preflight_uses_salad_reallocation_pattern() -> None:
    script = NETWORK_PREFLIGHT.read_text(encoding="utf-8")

    assert "https://speed.cloudflare.com/__down?bytes=" in script
    assert "SALAD_NETWORK_MIN_DOWNLOAD_MBPS" in script
    assert "SALAD_NETWORK_PREFLIGHT_PASS" in script
    assert "SALAD_NETWORK_PREFLIGHT_FAIL" in script
    assert "http://169.254.169.254/v1/reallocate" in script
    assert '"Metadata: true"' in script


def test_heavy_gpu_workers_require_fast_network_and_rtx5090_high_priority() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    for name in ("qwen_image_21", "ltx25"):
        service = manifest["services"][name]
        assert service["priority"] == "high"
        assert service["resources"]["gpu_class_names"] == ["RTX 5090 (32 GB)"]
        assert service["environment"]["SALAD_NETWORK_MIN_DOWNLOAD_MBPS"] == "100"
        assert service["environment"]["SALAD_NETWORK_TEST_BYTES"] == "25000000"
        assert service["environment"]["SALAD_NETWORK_TEST_ATTEMPTS"] == "3"
