"""Contracts for the LTX baked-weight image; never download weights in CI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ai_video_factory.workers.ltx25.model_manifest import (
    write_installed_model_manifest,
)

DOCKERFILE = Path("docker/workers/ltx25/Dockerfile")
BOOTSTRAP = Path("docker/workers/ltx25/download_models.sh")
MANAGER = Path("scripts/salad/manage_salad_worker.ps1")
MANIFEST = Path("docker/workers/ltx25/model-manifest.json")
SERVICE = Path("deploy/salad/services.json")


def test_ltx_build_bakes_exactly_pinned_manifest_as_unprivileged_worker() -> None:
    docker = DOCKERFILE.read_text(encoding="utf-8")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    service = json.loads(SERVICE.read_text(encoding="utf-8"))["services"]["ltx25"]

    assert manifest["repository"] == service["environment"]["LTX_MODEL_REPOSITORY"]
    assert manifest["revision"] == service["environment"]["LTX_MODEL_REVISION"]
    assert service["environment"]["LTX_INCLUDE_A2V_DEV_ASSETS"] == "false"
    assert service["environment"]["LTX_REQUIRE_BAKED_MODELS"] == "true"
    assert "LTX_REQUIRE_BAKED_MODELS=true" in docker
    assert "ARG LTX_BAKE_A2V_DEV_ASSETS=false" in docker
    assert 'export LTX_MODEL_BOOTSTRAP_CONTEXT=build' in docker
    assert docker.index("USER worker") < docker.index(
        "RUN --mount=type=secret,id=hf_token"
    )
    assert "/usr/local/bin/download-models;" in docker
    assert "LTX_MODEL_REVISION=" + manifest["revision"] in docker
    assert "ARG HF_TOKEN" not in docker
    assert "ENV HF_TOKEN" not in docker
    assert "COPY --from=" not in docker


def test_ltx_buildkit_token_is_secret_and_only_passed_for_ltx() -> None:
    manager = MANAGER.read_text(encoding="utf-8")
    docker = DOCKERFILE.read_text(encoding="utf-8")

    assert 'if ($Service -eq "ltx25") {' in manager
    assert 'IsNullOrWhiteSpace($env:HF_TOKEN)' in manager
    assert '--secret "id=hf_token,env=HF_TOKEN"' in manager
    assert '--build-arg HF_TOKEN' not in manager
    assert "--mount=type=secret,id=hf_token,required=false,uid=10001,gid=10001,mode=0400" in docker
    assert 'export HF_TOKEN="$(cat /run/secrets/hf_token)"' in docker


def test_ltx_build_only_skips_salad_network_watchdog_in_build_context() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    assert 'BOOTSTRAP_CONTEXT="${LTX_MODEL_BOOTSTRAP_CONTEXT:-runtime}"' in bootstrap
    assert 'if [[ "${BOOTSTRAP_CONTEXT}" == "runtime" ]]; then' in bootstrap
    assert "/usr/local/bin/network-preflight" in bootstrap
    assert 'WATCHDOG_REALLOCATION_ARGS=(--reallocate-on-slow)' in bootstrap
    assert 'WATCHDOG_REALLOCATION_ARGS=()' in bootstrap
    assert 'MIN_THROUGHPUT_MIBPS=0' in bootstrap
    assert '"${WATCHDOG_REALLOCATION_ARGS[@]}"' in bootstrap


def test_runtime_rejects_corrupt_baked_weights_before_any_network(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ltx-model"
    relative = "vae/tiny.safetensors"
    model = root / relative
    model.parent.mkdir(parents=True)
    model.write_bytes(b"good")
    installed = root / ".bordell-installed-model-manifest.json"
    write_installed_model_manifest(
        installed,
        repository="test/ltx",
        revision="pinned",
        root=root,
        files=[relative],
    )
    model.write_bytes(b"evil")  # Same size: must not accept a stale manifest.
    expected_manifest = installed.read_bytes()
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps(
            {
                "repository": "test/ltx",
                "revision": "pinned",
                "shared_files": [relative],
                "dev_files": [],
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "LTX_MODEL_MANIFEST": str(expected),
        "LTX_MODEL_ROOT": str(root),
        "LTX_MODEL_REPOSITORY": "test/ltx",
        "LTX_MODEL_REVISION": "pinned",
        "LTX_REQUIRE_BAKED_MODELS": "true",
        "LTX_MODEL_BOOTSTRAP_CONTEXT": "runtime",
    }
    result = subprocess.run(
        ["bash", str(BOOTSTRAP)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode != 0
    assert "BAKED_MODEL_MANIFEST_INVALID" in result.stderr
    assert "MODEL_VERIFY_START" not in result.stdout
    assert "SALAD_NETWORK_PREFLIGHT" not in result.stdout
    assert installed.read_bytes() == expected_manifest
