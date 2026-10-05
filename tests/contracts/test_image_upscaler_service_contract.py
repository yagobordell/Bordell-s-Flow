from __future__ import annotations

import json
from pathlib import Path

BASE_IMAGE = (
    "ghcr.io/yagobordell/image-upscaler@"
    "sha256:00e046a7ba6dd00d406609f2f5c007b176b96578b1baac612ea8e97940c35aba"
)
DOCKERFILE = Path("docker/services/image-upscaler-salad/Dockerfile")
ENTRYPOINT = Path("docker/services/image-upscaler-salad/entrypoint.sh")
MANIFEST = Path("deploy/salad/image-upscaler-service.json")
WORKER_MANIFEST = Path("deploy/salad/services.json")
DEPLOY_SCRIPT = Path("scripts/salad/manage_image_upscaler_service.ps1")


def test_salad_adapter_wraps_exact_optium_image_without_copying_runtime_code() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")

    assert f"FROM {BASE_IMAGE}" in dockerfile
    assert "apt-get install -y --no-install-recommends socat" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "RUNTIME_HOST=127.0.0.1" in dockerfile
    assert "RUNTIME_PORT=8081" in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert "python -m image_upscaler" in entrypoint
    assert "socat TCP6-LISTEN:8080,reuseaddr,fork TCP4:127.0.0.1:8081" in entrypoint


def test_image_upscaler_has_independent_salad_group_contract() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    service = manifest["service"]

    assert manifest["schema_version"] == 1
    assert service["group_name"] == "bordell-image-upscaler-v2"
    assert service["image"].startswith("ghcr.io/yagobordell/image-upscaler-salad:")
    assert service["capacity"]["replicas"] == 1
    assert service["resources"] == {
        "cpu": 4,
        "memory": 16384,
        "gpu_class_names": ["RTX 5090 (32 GB)"],
    }
    assert service["networking"] == {"protocol": "http", "port": 8080, "auth": True}
    assert service["probes"]["startup"]["path"] == "/health"
    assert service["probes"]["readiness"]["path"] == "/ready"
    assert service["probes"]["liveness"]["path"] == "/health"
    assert all(probe["port"] == 8080 for probe in service["probes"].values())
    assert all(1 <= probe["failure_threshold"] <= 20 for probe in service["probes"].values())
    assert service["probes"]["readiness"]["period_seconds"] == 30
    assert service["probes"]["readiness"]["failure_threshold"] == 20
    assert service["probes"]["liveness"]["period_seconds"] == 60
    assert service["probes"]["liveness"]["failure_threshold"] == 20

    worker_manifest = json.loads(WORKER_MANIFEST.read_text(encoding="utf-8"))
    assert "image_upscaler" not in worker_manifest["services"]
    assert service["group_name"] not in {
        definition["group_name"] for definition in worker_manifest["services"].values()
    }


def test_image_upscaler_deploy_uses_private_ghcr_basic_auth_without_committed_pat() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert '"GHCR_READ_PACKAGES_PAT"' in script
    assert "registry_authentication" in script
    assert 'username = "yagobordell"' in script
    assert "networking =" in script
    assert "auth = $true" in script
    assert 'ContentType "application/json"' in script
    assert 'ContentType "application/merge-patch+json"' in script
    assert "Wait-SaladGroupSettled" in script
    assert "pending_change" in script
    assert "Test-InsecureRedirectError" in script
    assert "retrying the canonical " in script
    assert "personal_access_token" not in script
    assert "ghp_" not in script
