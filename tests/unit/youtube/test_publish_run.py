"""Recovery CLI accepts only verified finished publications; no generation calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.youtube import publish_run


@pytest.mark.parametrize("schema", [
    "managed-b-pipeline-v1", "b-pipeline-publication-v1",
])
def test_publish_run_delegates_verified_mp4_without_generation(
    tmp_path: Path, monkeypatch, schema: str,
):
    result = tmp_path / "result"
    result.mkdir()
    video = result / "guion.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\0" * 32)
    (result / "manifest.json").write_text(json.dumps({
        "schema_version": schema,
        "files": {"guion.mp4": hashlib.sha256(video.read_bytes()).hexdigest()},
    }))
    cli = Mock(return_value=0)
    monkeypatch.setattr(publish_run, "upload_main", cli)
    command = [
        "--result-dir", str(result), "--channel", "test01",
        "--metadata", str(tmp_path / "custom.json"),
        "--thumbnail", str(tmp_path / "image.png"), "--dry-run",
    ]
    assert publish_run.main(command) == 0
    arguments = cli.call_args.args[0]
    assert arguments[:4] == [
        "--video", str(video), "--metadata", str(tmp_path / "custom.json"),
    ]
    assert "--dry-run" in arguments
    assert "--channel" in arguments
    assert "--thumbnail" in arguments


@pytest.mark.parametrize("kind", [
    "missing", "invalid_schema", "missing_video", "changed_hash", "multiple_video",
])
def test_publish_run_refuses_unverified_results(
    tmp_path: Path, monkeypatch, kind: str,
):
    result = tmp_path / "result"
    result.mkdir()
    video = result / "guion.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\0" * 32)
    manifest = {
        "schema_version": "managed-b-pipeline-v1",
        "files": {"guion.mp4": hashlib.sha256(video.read_bytes()).hexdigest()},
    }
    if kind == "invalid_schema":
        manifest["schema_version"] = "unknown"
    elif kind == "missing_video":
        video.unlink()
    elif kind == "changed_hash":
        manifest["files"]["guion.mp4"] = "a" * 64
    elif kind == "multiple_video":
        manifest["files"]["extra.mp4"] = "b" * 64
    if kind != "missing":
        (result / "manifest.json").write_text(json.dumps(manifest))
    call = Mock(side_effect=AssertionError("Must never initiate publish"))
    monkeypatch.setattr(publish_run, "upload_main", call)
    with pytest.raises(SystemExit) as err:
        publish_run.main(["--result-dir", str(result), "--dry-run"])
    assert err.value.code == 2
    call.assert_not_called()
