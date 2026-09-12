import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


DOWNLOADER = Path("docker/workers/ideogram4/download_models.sh")


def test_ideogram_container_uses_downloaded_local_snapshot() -> None:
    dockerfile = Path("docker/workers/ideogram4/Dockerfile").read_text(encoding="utf-8")
    downloader = DOWNLOADER.read_text(encoding="utf-8")

    assert (
        "IDEOGRAM_MODEL_LOCAL_SNAPSHOT=/workspace/models/ideogram4/snapshot"
        in dockerfile
    )
    assert 'hub_call = "hf_hub_download("' in dockerfile
    assert "text.count(hub_call) != 6" in dockerfile
    assert 'text = text.replace(hub_call, "_resolve_model_file(")' in dockerfile
    assert "def _resolve_model_file(repo_id: str, filename: str) -> str:" in dockerfile
    assert "IDEOGRAM_MODEL_LOCAL_SNAPSHOT" in dockerfile
    assert "config.weights_repo = str(local_snapshot)" in dockerfile

    assert 'snapshot_dir="${IDEOGRAM_MODEL_LOCAL_SNAPSHOT:-${model_root}/snapshot}"' in downloader
    assert '--local-dir "${snapshot_dir}"' in downloader
    assert 'ln -s "${snapshot_path}"' not in downloader
    assert "transformer/diffusion_pytorch_model.safetensors" in downloader
    assert "unconditional_transformer/diffusion_pytorch_model.safetensors" in downloader
    assert downloader.index('--local-dir "${snapshot_dir}"') < downloader.index(
        'printf \'%s@%s\\n\' "${repo}" "${revision}" > "${model_root}/.ready"'
    )


@pytest.mark.skipif(sys.platform == "win32", reason="shell behavior is covered by Linux CI")
def test_ideogram_downloader_materializes_snapshot_before_ready(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_hf = fake_bin / "hf"
    fake_hf.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$1" == "download" ]]
shift
shift
local_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --revision)
      shift 2
      ;;
    --local-dir)
      local_dir="$2"
      shift 2
      ;;
    *)
      echo "unexpected hf argument: $1" >&2
      exit 2
      ;;
  esac
done
[[ -n "${local_dir}" ]]
mkdir -p "${local_dir}/transformer" "${local_dir}/unconditional_transformer"
printf 'conditional' > "${local_dir}/transformer/diffusion_pytorch_model.safetensors"
unconditional_path="${local_dir}/unconditional_transformer/diffusion_pytorch_model.safetensors"
printf 'unconditional' > "${unconditional_path}"
printf '%s\n' "${local_dir}"
""",
        encoding="utf-8",
    )
    fake_hf.chmod(0o755)

    model_root = tmp_path / "model"
    snapshot_dir = model_root / "snapshot"
    environment = os.environ.copy()
    environment.update(
        {
            "HF_TOKEN": "test-token",
            "IDEOGRAM_MODEL_REPOSITORY": "example/ideogram",
            "IDEOGRAM_MODEL_REVISION": "test-revision",
            "IDEOGRAM_MODEL_ROOT": str(model_root),
            "IDEOGRAM_MODEL_LOCAL_SNAPSHOT": str(snapshot_dir),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )

    completed = subprocess.run(
        [shutil.which("bash") or "bash", str(DOWNLOADER)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert (snapshot_dir / "transformer/diffusion_pytorch_model.safetensors").is_file()
    assert (
        snapshot_dir / "unconditional_transformer/diffusion_pytorch_model.safetensors"
    ).is_file()
    assert (model_root / ".ready").read_text(encoding="utf-8") == (
        "example/ideogram@test-revision\n"
    )
    assert "Ideogram 4 model bootstrap complete: example/ideogram@test-revision" in (
        completed.stdout
    )


@pytest.mark.skipif(sys.platform == "win32", reason="shell behavior is covered by Linux CI")
def test_ideogram_downloader_does_not_mark_partial_snapshot_ready(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_hf = fake_bin / "hf"
    fake_hf.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
local_dir=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--local-dir" ]]; then
    local_dir="$2"
    break
  fi
  shift
done
[[ -n "${local_dir}" ]]
mkdir -p "${local_dir}/transformer"
printf 'conditional' > "${local_dir}/transformer/diffusion_pytorch_model.safetensors"
printf '%s\n' "${local_dir}"
""",
        encoding="utf-8",
    )
    fake_hf.chmod(0o755)

    model_root = tmp_path / "model"
    snapshot_dir = model_root / "snapshot"
    environment = os.environ.copy()
    environment.update(
        {
            "HF_TOKEN": "test-token",
            "IDEOGRAM_MODEL_ROOT": str(model_root),
            "IDEOGRAM_MODEL_LOCAL_SNAPSHOT": str(snapshot_dir),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )

    completed = subprocess.run(
        [shutil.which("bash") or "bash", str(DOWNLOADER)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode != 0
    assert "unconditional_transformer/diffusion_pytorch_model.safetensors" in completed.stderr
    assert not (model_root / ".ready").exists()
