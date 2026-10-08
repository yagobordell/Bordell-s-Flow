from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.workflows import r2_cleanup


def _finished(tmp_path: Path) -> tuple[Path, dict]:
    output = tmp_path / "video"
    run = output / "ltx" / "runs" / "abc"
    run.mkdir(parents=True)
    mp4 = output / "final.mp4"
    mp4.write_bytes(b"video")
    sha = hashlib.sha256(b"video").hexdigest()
    data = {
        "status": "completed", "fingerprint": "abc", "sha256": sha,
        "file": "final.mp4",
        "jobs": [{"status": "completed", "job_id": "b2-shot-1"}],
    }
    (run / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return output, {key: data[key] for key in ("status", "fingerprint", "sha256", "file")} | {
        "manifest_file": "ltx/runs/abc/manifest.json"
    }


def test_cleanup_only_targets_manifest_job_prefix(tmp_path: Path, monkeypatch) -> None:
    output, video = _finished(tmp_path)
    objects = {
        "jobs/b2-shot-1/clip.mp4", "jobs/another-run/clip.mp4",
        "voices/project-narrator-v1.wav", "phase8/keyframes/shared.png",
    }
    class Client:
        def list_objects_v2(self, *, Bucket, Prefix, MaxKeys):
            return {"Contents": [{"Key": k} for k in sorted(objects) if k.startswith(Prefix)]}
        def delete_objects(self, *, Bucket, Delete):
            removed = [item["Key"] for item in Delete["Objects"]]
            objects.difference_update(removed)
            return {"Deleted": [{"Key": k} for k in removed]}

    monkeypatch.setattr(
        r2_cleanup, "create_r2_storage",
        lambda **kwargs: SimpleNamespace(_client=Client(), bucket="test"),
    )
    assert r2_cleanup.cleanup_published_video_jobs(output, video) == 1
    assert objects == {
        "jobs/another-run/clip.mp4",
        "voices/project-narrator-v1.wav", "phase8/keyframes/shared.png",
    }
    assert r2_cleanup.cleanup_published_video_jobs(output, video) == 0


def test_cleanup_rejects_unverified_or_incomplete_runs(tmp_path: Path, monkeypatch) -> None:
    output, video = _finished(tmp_path)
    monkeypatch.setattr(
        r2_cleanup, "create_r2_storage",
        lambda **kwargs: pytest.fail("R2 must not be contacted"),
    )
    assert r2_cleanup.cleanup_published_video_jobs(output, {**video, "status": "deferred"}) == 0
    with pytest.raises(ValueError):
        r2_cleanup.cleanup_published_video_jobs(output, {**video, "sha256": "bad"})
    with pytest.raises(ValueError):
        r2_cleanup.cleanup_published_video_jobs(output, {**video, "manifest_file": "../other.json"})
    manifest = output / video["manifest_file"]
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["jobs"][0]["job_id"] = "../voices/project-narrator-v1.wav"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        r2_cleanup.cleanup_published_video_jobs(output, video)
