from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_salad_adapter_is_pinned_thin_ipv6_bridge() -> None:
    dockerfile = (
        _REPO_ROOT / "docker/services/image-upscaler-salad/Dockerfile"
    ).read_text(encoding="utf-8")
    entrypoint = (
        _REPO_ROOT / "docker/services/image-upscaler-salad/entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert (
        "FROM ghcr.io/yagobordell/image-upscaler@"
        "sha256:00e046a7ba6dd00d406609f2f5c007b176b96578b1baac612ea8e97940c35aba"
    ) in dockerfile
    assert "apt-get install -y --no-install-recommends socat" in dockerfile
    assert "RUNTIME_HOST=127.0.0.1" in dockerfile
    assert "RUNTIME_PORT=8081" in dockerfile
    assert "python -m image_upscaler" in entrypoint
    assert (
        "socat TCP6-LISTEN:8080,reuseaddr,fork TCP4:127.0.0.1:8081"
        in entrypoint
    )


def test_salad_manifest_keeps_upscaler_independent() -> None:
    manifest = json.loads(
        (_REPO_ROOT / "deploy/salad/image-upscaler-service.json").read_text(
            encoding="utf-8"
        )
    )
    service = manifest["service"]

    assert service["source_image"].endswith(
        "@sha256:00e046a7ba6dd00d406609f2f5c007b176b96578b1baac612ea8e97940c35aba"
    )
    assert service["capacity"]["replicas"] == 1
    assert service["resources"]["cpu"] == 4
    assert service["resources"]["memory"] == 16384
    assert service["resources"]["gpu_class_names"] == ["RTX 5090 (32 GB)"]
    assert service["networking"] == {"protocol": "http", "port": 8080, "auth": True}
    assert service["probes"]["startup"]["path"] == "/health"
    assert service["probes"]["readiness"]["path"] == "/ready"
    assert service["probes"]["liveness"]["path"] == "/health"
