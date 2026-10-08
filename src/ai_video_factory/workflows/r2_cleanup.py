"""Safely reclaim run-private R2 outputs after publishing a verified MP4.

Legacy GPU requests use globally shared keys and must never be garbage-collected
by this path. New LTX jobs use an isolated run scope and private inputs.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.providers.r2 import create_r2_storage

_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
_SCOPE = re.compile(r"^[a-f0-9]{24}$")


def _read_scoped_manifest(output: Path, video: dict[str, Any]) -> tuple[list[str], str]:
    relative = video.get("manifest_file")
    if not isinstance(relative, str) or not relative:
        raise ValueError("Video cleanup needs the LTX manifest")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Unsafe LTX manifest path")
    root = output.resolve()
    manifest_path = root / candidate
    if (
        manifest_path.is_symlink()
        or not manifest_path.resolve().is_relative_to(root)
        or not manifest_path.is_file()
    ):
        raise ValueError("Unsafe or missing LTX manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if any(
        manifest.get(field) != video.get(field)
        for field in ("status", "fingerprint", "sha256", "file")
    ):
        raise ValueError("Video manifest does not match published final MP4")

    marker = output / "video" / "r2_run_scope.txt"
    if marker.is_symlink() or marker.parent.is_symlink():
        raise ValueError("Unsafe video scope marker")
    if not marker.is_file():
        # Legacy deterministic GPU keys are shared by other video runs.
        return [], ""
    scope = marker.read_text(encoding="ascii").strip()
    if not _SCOPE.fullmatch(scope):
        raise ValueError("Invalid run-private R2 scope")
    records = manifest.get("jobs")
    if not isinstance(records, list) or not records:
        raise ValueError("Video manifest has no completed GPU jobs")
    job_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "completed":
            raise ValueError("Cannot clean an incomplete GPU job")
        job_id = record.get("job_id")
        if (
            not isinstance(job_id, str)
            or not _JOB_ID.fullmatch(job_id)
            or not job_id.endswith(f"-r{scope}")
        ):
            raise ValueError("Manifest contains a non-run-private GPU job")
        job_ids.append(job_id)
    if len(set(job_ids)) != len(job_ids):
        raise ValueError("Duplicate GPU job identity in video manifest")
    return job_ids, scope


def cleanup_published_video_jobs(output: Path, video: dict[str, Any]) -> int:
    """Remove only isolated LTX input/output bundles; retain global caches.

    The verified final MP4 remains local. Postgres row eviction is committed
    only after successful R2 deletion so future submissions cannot reuse a
    succeeded queue row whose cached video was removed.
    """
    if video.get("status") != "completed":
        return 0
    from ai_video_factory.compositor.static_preview import _verified_asset

    _verified_asset(output, video.get("file"), video.get("sha256"))
    job_ids, scope = _read_scoped_manifest(output, video)
    if not scope:
        return 0
    receipt = output / "video" / "r2_cleanup.json"
    if receipt.is_symlink():
        raise ValueError("Refusing linked R2 cleanup receipt")
    if receipt.is_file():
        completed = json.loads(receipt.read_text(encoding="utf-8"))
        if completed.get("scope") == scope and completed.get("sha256") == video["sha256"]:
            return 0

    queue = PostgresJobQueueClient(dsn=settings.postgres_dsn)
    try:
        storage = create_r2_storage(
            endpoint_url=settings.r2_endpoint_url,
            bucket=settings.r2_bucket,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key,
        )
        client = storage._client
        deleted = 0
        with queue.prune_scoped_completed_video_jobs(job_ids):
            for job_id in sorted(job_ids):
                prefix = f"jobs/{job_id}/"
                while True:
                    response = client.list_objects_v2(
                        Bucket=storage.bucket, Prefix=prefix, MaxKeys=1000,
                    )
                    keys = [
                        {"Key": item["Key"]}
                        for item in response.get("Contents", [])
                        if item["Key"].startswith(prefix)
                    ]
                    if not keys:
                        break
                    result = client.delete_objects(
                        Bucket=storage.bucket,
                        Delete={"Objects": keys, "Quiet": False},
                    )
                    errors = result.get("Errors", [])
                    if errors:
                        raise RuntimeError(
                            f"R2 cleanup failed for scoped job {job_id}: "
                            f"{len(errors)} deletion errors"
                        )
                    completed = len(result.get("Deleted", []))
                    if completed != len(keys):
                        raise RuntimeError("R2 cleanup made incomplete deletion progress")
                    deleted += completed
        # Durable, idempotent completion marker for repeat publication/resume.
        temporary = receipt.with_suffix(".part.json")
        if temporary.is_symlink():
            raise ValueError("Refusing linked R2 cleanup receipt temporary file")
        temporary.write_text(
            json.dumps({
                "scope": scope, "sha256": video["sha256"],
                "deleted_objects": deleted, "status": "completed",
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, receipt)
        return deleted
    finally:
        queue.close()
