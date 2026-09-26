from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.workers.ltx25 import DirectLTX25Backend, LTXModelFiles
from ai_video_factory.workers.ltx25.a2v import DirectLTX25AudioToVideoBackend
from ai_video_factory.workers.ltx25.model_manifest import (
    installed_model_manifest_ready,
    model_bootstrap_is_ready,
    require_ltx_model_bootstrap,
    write_installed_model_manifest,
)

REPOSITORY = "Lightricks/LTX-2.5"
REVISION = "6c7e5e573ac1667efc83407806fe9b0b93730e60"


def _seed(root: Path) -> list[str]:
    files = LTXModelFiles.from_root(root)
    result: list[str] = []
    for path in files.paths():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"verified-model")
        result.append(path.relative_to(root).as_posix())
    return result


def _receipt(root: Path, names: list[str]) -> Path:
    path = root / ".bordell-installed-model-manifest.json"
    write_installed_model_manifest(
        path,
        repository=REPOSITORY,
        revision=REVISION,
        root=root,
        files=names,
    )
    return path


def _enable_gate(monkeypatch: pytest.MonkeyPatch, marker: Path) -> None:
    monkeypatch.setenv("LTX_REQUIRE_VERIFIED_MODEL_MANIFEST", "true")
    monkeypatch.setenv("LTX_MODEL_BOOTSTRAP_COMPLETE_FILE", str(marker))
    monkeypatch.setenv("LTX_MODEL_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("LTX_MODEL_REVISION", REVISION)


def test_no_worker_readiness_before_both_atomic_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "models"
    names = _seed(root)
    marker = tmp_path / "boot.complete"
    _enable_gate(monkeypatch, marker)

    with pytest.raises(FileNotFoundError, match="bootstrap is not complete"):
        require_ltx_model_bootstrap(root=root, expected_files=names)

    _receipt(root, names)
    assert installed_model_manifest_ready(
        root / ".bordell-installed-model-manifest.json",
        repository=REPOSITORY,
        revision=REVISION,
        root=root,
        expected_files=names,
    )
    with pytest.raises(FileNotFoundError, match="per-start completion marker"):
        require_ltx_model_bootstrap(root=root, expected_files=names)

    marker.write_text("old-revision\n", encoding="utf-8")
    assert not model_bootstrap_is_ready(
        root=root,
        repository=REPOSITORY,
        revision=REVISION,
        expected_files=names,
        completion_marker=marker,
    )

    marker.write_text(f"{REVISION}\n", encoding="utf-8")
    require_ltx_model_bootstrap(root=root, expected_files=names)
    (root / names[0]).write_bytes(b"truncated")
    with pytest.raises(FileNotFoundError, match="bootstrap is not complete"):
        require_ltx_model_bootstrap(root=root, expected_files=names)


def test_model_receipt_requires_exact_files_and_revision(tmp_path: Path) -> None:
    names = _seed(tmp_path)
    receipt = _receipt(tmp_path, names)
    kwargs = {
        "installed_path": receipt,
        "repository": REPOSITORY,
        "revision": REVISION,
        "root": tmp_path,
        "expected_files": names,
    }
    assert installed_model_manifest_ready(**kwargs)
    assert not installed_model_manifest_ready(**{**kwargs, "revision": "other"})
    assert not installed_model_manifest_ready(**{**kwargs, "expected_files": names[:-1]})


def test_a2v_preparation_and_live_readiness_share_verified_bootstrap_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _ltx_a2v_support import make_a2v_bindings

    root = tmp_path / "models"
    names = _seed(root)
    marker = tmp_path / "boot.complete"
    _enable_gate(monkeypatch, marker)
    monkeypatch.setenv("LTX_INCLUDE_A2V_DEV_ASSETS", "false")
    backend = DirectLTX25AudioToVideoBackend(model_root=root)
    monkeypatch.setattr(backend, "_get_bindings", lambda: make_a2v_bindings({}))

    with pytest.raises(ModelBootstrapPendingError, match="bootstrap"):
        backend.prepare()
    _receipt(root, names)
    with pytest.raises(ModelBootstrapPendingError, match="bootstrap"):
        backend.prepare()
    marker.write_text(f"{REVISION}\n", encoding="utf-8")
    backend.prepare()
    backend.ready()

    marker.unlink()
    with pytest.raises(FileNotFoundError, match="bootstrap"):
        backend.ready()


def test_i2v_preparation_waits_for_same_verified_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "models"
    names = _seed(root)
    marker = tmp_path / "boot.complete"
    _enable_gate(monkeypatch, marker)
    backend = DirectLTX25Backend(model_root=root)
    bindings = SimpleNamespace(
        torch=SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    )
    monkeypatch.setattr(backend, "_get_bindings", lambda: bindings)
    monkeypatch.setattr(backend, "_get_or_build_pipeline", lambda _: object())

    with pytest.raises(ModelBootstrapPendingError, match="bootstrap"):
        backend.prepare()
    _receipt(root, names)
    marker.write_text(f"{REVISION}\n", encoding="utf-8")
    backend.prepare()
    backend.ready()
    marker.unlink()
    with pytest.raises(FileNotFoundError, match="bootstrap"):
        backend.ready()


def test_ltx_container_enables_gate_and_shell_publishes_it_last() -> None:
    dockerfile = Path("docker/workers/ltx25/Dockerfile").read_text(encoding="utf-8")
    shell = Path("docker/workers/ltx25/download_models.sh").read_text(encoding="utf-8")
    assert "LTX_REQUIRE_VERIFIED_MODEL_MANIFEST=true" in dockerfile
    assert "LTX_MODEL_BOOTSTRAP_COMPLETE_FILE=" in dockerfile
    assert 'rm -f -- "${BOOTSTRAP_COMPLETE_FILE}"' in shell
    assert shell.index("MODEL_MANIFEST_VALID path=") < shell.index(
        'mv -f -- "${completion_temp}" "${BOOTSTRAP_COMPLETE_FILE}"'
    )
    assert shell.index("MODEL_VERIFY_DONE") < shell.index(
        'mv -f -- "${completion_temp}" "${BOOTSTRAP_COMPLETE_FILE}"'
    )
    assert "LTX_BOOTSTRAP_COMPLETE revision=" in shell
