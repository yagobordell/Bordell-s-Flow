"""Clean R2 outputs belonging to a verified and published video run.

Shared content-addressed inputs are deliberately not deleted here: their
ownership cannot be inferred from the final video manifest alone.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.providers.r2 import create_r2_storage

_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def cleanup_published_video_jobs(output: Path, video: dict[str, Any]) -> int:
    """Delete only job-scoped outputs referenced by the verified final export.

    Never use bucket-wide or phase-wide deletion: content-addressed assets may
    be shared between concurrent runs. A failed cleanup is safe to retry.
    """
    if video.get("status") != "completed":
        return 0
    relative = video.get("manifest_file")
    if not isinstance(relative, str) or not relative:
        return 0
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("Invalid video manifest path")
    root = output.resolve()
    manifest_path = (root / relative_path).resolve()
    if not manifest_path.is_relative_to(root) or not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("Unsafe video manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "completed"
        or manifest.get("fingerprint") != video.get("fingerprint")
        or manifest.get("sha256") != video.get("sha256")
        or manifest.get("file") != video.get("file")
    ):
        raise ValueError("Video manifest does not match published video")
    from ai_video_factory.compositor.static_preview import _verified_asset

    _verified_asset(root, video["file"], video["sha256"])
    records = manifest.get("jobs")
    if not isinstance(records, list) or not records:
        return 0
    job_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "completed":
            raise ValueError("Refusing to clean an incomplete job")
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("Unsafe job identifier")
        job_ids.add(job_id)
    storage = create_r2_storage(
        endpoint_url=settings.r2_endpoint_url,
        bucket=settings.r2_bucket,
        access_key_id=settings.r2_access_key_id,
        secret_access_key=settings.r2_secret_access_key,
    )
    client = storage._client
    deleted = 0
    for job_id in sorted(job_ids):
        prefix = f"jobs/{job_id}/"
        while True:
            response = client.list_objects_v2(Bucket=storage.bucket, Prefix=prefix, MaxKeys=1000)
            keys = [{"Key": item["Key"]} for item in response.get("Contents", []) if item["Key"].startswith(prefix)]
            if not keys:
                break
            result = client.delete_objects(
                Bucket=storage.bucket, Delete={"Objects": keys, "Quiet": False}
            )
            if result.get("Errors"):
                raise RuntimeError(f"R2 job cleanup failed for {job_id}: {len(result['Errors'])} errors")
            deleted += len(result.get("Deleted", []))
    return deleted
