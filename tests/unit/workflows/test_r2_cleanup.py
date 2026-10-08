from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.compositor.static_preview import StaticPreviewError
from ai_video_factory.workflows import r2_cleanup


_SCOPE = "a" * 24
_JOB_ID = f"b2-shot-1-r{_SCOPE}"


def _finished(tmp_path: Path) -> tuple[Path, dict]:
    output = tmp_path / "video"
    run = output / "video" / "runs" / "abc"
    run.mkdir(parents=True)
    (output / "video" / "r2_run_scope.txt").write_text(_SCOPE, encoding="ascii")
    mp4 = output / "final.mp4"
    mp4.write_bytes(b"video")
    sha = hashlib.sha256(b"video").hexdigest()
    data = {
        "status": "completed", "fingerprint": "abc", "sha256": sha,
        "file": "final.mp4",
        "jobs": [{"status": "completed", "job_id": _JOB_ID}],
    }
    (run / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return output, {
        key: data[key] for key in ("status", "fingerprint", "sha256", "file")
    } | {"manifest_file": "video/runs/abc/manifest.json"}


def test_cleanup_only_targets_private_job_and_invalidates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output, video = _finished(tmp_path)
    objects = {
        f"jobs/{_JOB_ID}/clip.mp4",
        f"jobs/{_JOB_ID}/inputs/image.png",
        f"jobs/{_JOB_ID}/metadata.json",
        "jobs/another-run/clip.mp4",
        "voices/project-narrator-v1.wav",
        "b2-ltx/inputs/images/shared.png",
    }
    evicted: list[str] = []
    class Client:
        def list_objects_v2(self, *, Bucket, Prefix, MaxKeys):
            return {"Contents": [
                {"Key": key} for key in sorted(objects) if key.startswith(Prefix)
            ]}

        def delete_objects(self, *, Bucket, Delete):
            removed = [item["Key"] for item in Delete["Objects"]]
            objects.difference_update(removed)
            return {"Deleted": [{"Key": key} for key in removed]}

    class Queue:
        @contextmanager
        def prune_scoped_video_run(self, scope, job_ids):
            assert scope == _SCOPE
            assert job_ids == [_JOB_ID]
            yield job_ids
            evicted.extend(job_ids)

        def close(self):
            pass

    monkeypatch.setattr(
        r2_cleanup, "create_r2_storage",
        lambda **kwargs: SimpleNamespace(_client=Client(), bucket="test"),
    )
    monkeypatch.setattr(r2_cleanup, "PostgresJobQueueClient", lambda **kwargs: Queue())
    assert r2_cleanup.cleanup_published_video_jobs(output, video) == 3
    assert evicted == [_JOB_ID]
    assert objects == {
        "jobs/another-run/clip.mp4",
        "voices/project-narrator-v1.wav",
        "b2-ltx/inputs/images/shared.png",
    }
    assert r2_cleanup.cleanup_published_video_jobs(output, video) == 0
    assert evicted == [_JOB_ID]


def test_cleanup_rejects_unverified_or_incomplete_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output, video = _finished(tmp_path)
    monkeypatch.setattr(
        r2_cleanup, "create_r2_storage",
        lambda **kwargs: pytest.fail("R2 must not be contacted"),
    )
    assert r2_cleanup.cleanup_published_video_jobs(
        output, {**video, "status": "deferred"},
    ) == 0
    with pytest.raises(StaticPreviewError):
        r2_cleanup.cleanup_published_video_jobs(output, {**video, "sha256": "bad"})
    with pytest.raises(ValueError):
        r2_cleanup.cleanup_published_video_jobs(
            output, {**video, "manifest_file": "../other.json"},
        )
    manifest = output / video["manifest_file"]
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["jobs"][0]["job_id"] = "fish-speech-original"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="non-run-private"):
        r2_cleanup.cleanup_published_video_jobs(output, video)


def test_legacy_global_video_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output, video = _finished(tmp_path)
    (output / "video" / "r2_run_scope.txt").unlink()
    monkeypatch.setattr(
        r2_cleanup, "create_r2_storage",
        lambda **kwargs: pytest.fail("Legacy R2 must not be touched"),
    )
    assert r2_cleanup.cleanup_published_video_jobs(output, video) == 0


def test_delete_error_keeps_queue_and_does_not_write_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output, video = _finished(tmp_path)
    committed: list[str] = []

    class BrokenClient:
        def list_objects_v2(self, **kwargs):
            return {"Contents": [{"Key": f"jobs/{_JOB_ID}/clip.mp4"}]}

        def delete_objects(self, **kwargs):
            return {"Errors": [{"Key": "clip.mp4"}]}

    class Queue:
        @contextmanager
        def prune_scoped_video_run(self, scope, job_ids):
            yield job_ids
            committed.extend(job_ids)

        def close(self):
            pass

    monkeypatch.setattr(r2_cleanup, "PostgresJobQueueClient", lambda **kwargs: Queue())
    monkeypatch.setattr(
        r2_cleanup, "create_r2_storage",
        lambda **kwargs: SimpleNamespace(_client=BrokenClient(), bucket="test"),
    )
    with pytest.raises(RuntimeError, match="deletion errors"):
        r2_cleanup.cleanup_published_video_jobs(output, video)
    assert committed == []
    assert not (output / "video" / "r2_cleanup.json").exists()
