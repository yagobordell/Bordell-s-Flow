from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


ARTIFACTS = (
    ("status.json", "application/json"),
    ("benchmark.log", "text/plain"),
    ("rtx5090/matrix.json", "application/json"),
    ("rtx5090/distilled-fp8-cpu.json", "application/json"),
    ("videos/rtx5090/distilled-fp8-cpu.mp4", "video/mp4"),
)


def _sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _object_key(prefix: str, relative_path: str) -> str:
    clean_prefix = prefix.strip("/")
    clean_relative = str(PurePosixPath(relative_path))
    return f"{clean_prefix}/{clean_relative}" if clean_prefix else clean_relative


def _upload_file(
    client: Any,
    *,
    source: Path,
    bucket: str,
    key: str,
    content_type: str,
) -> dict[str, object]:
    size_bytes = source.stat().st_size
    sha256 = _sha256(source)
    metadata = {"sha256": sha256, "phase": "7", "hardware": "rtx5090"}
    client.upload_file(
        str(source),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type, "Metadata": metadata},
    )
    remote = client.head_object(Bucket=bucket, Key=key)
    if int(remote["ContentLength"]) != size_bytes:
        raise RuntimeError(f"R2 size mismatch after upload: {key}")
    remote_sha256 = str(remote.get("Metadata", {}).get("sha256", ""))
    if remote_sha256 != sha256:
        raise RuntimeError(f"R2 SHA-256 metadata mismatch after upload: {key}")
    return {
        "key": key,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "etag": str(remote.get("ETag", "")).strip('"') or None,
    }


def upload_results(
    client: Any,
    *,
    results_root: Path,
    bucket: str,
    prefix: str,
) -> dict[str, object]:
    objects: list[dict[str, object]] = []
    for relative_path, content_type in ARTIFACTS:
        source = results_root / relative_path
        if not source.is_file() or source.stat().st_size <= 0:
            raise FileNotFoundError(f"benchmark artifact is missing or empty: {source}")
        key = _object_key(prefix, relative_path)
        objects.append(
            _upload_file(
                client,
                source=source,
                bucket=bucket,
                key=key,
                content_type=content_type,
            )
        )
        print(f"R2_UPLOAD_OK key={key} bytes={source.stat().st_size}", flush=True)

    manifest = {
        "schema_version": "1",
        "phase": 7,
        "hardware": "rtx5090",
        "created_at": datetime.now(UTC).isoformat(),
        "bucket": bucket,
        "prefix": prefix.strip("/"),
        "objects": objects,
    }
    manifest_path = results_root / "r2-upload-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_key = _object_key(prefix, manifest_path.name)
    manifest["manifest"] = _upload_file(
        client,
        source=manifest_path,
        bucket=bucket,
        key=manifest_key,
        content_type="application/json",
    )
    print(f"R2_UPLOAD_OK key={manifest_key} bytes={manifest_path.stat().st_size}", flush=True)
    return manifest


def _required(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"missing required setting: {name}")
    return value.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Persist Phase 7 benchmark artifacts in R2.")
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--endpoint-url", default=os.getenv("R2_ENDPOINT_URL"))
    parser.add_argument("--bucket", default=os.getenv("R2_BUCKET"))
    parser.add_argument("--access-key-id", default=os.getenv("R2_ACCESS_KEY_ID"))
    parser.add_argument("--secret-access-key", default=os.getenv("R2_SECRET_ACCESS_KEY"))
    parser.add_argument(
        "--prefix",
        default=os.getenv(
            "PHASE7_BENCHMARK_R2_PREFIX",
            "phase7/benchmarks/rtx5090-cloud",
        ),
    )
    args = parser.parse_args()

    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=_required(args.endpoint_url, "R2_ENDPOINT_URL"),
        aws_access_key_id=_required(args.access_key_id, "R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required(args.secret_access_key, "R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    upload_results(
        client,
        results_root=args.results_root.resolve(),
        bucket=_required(args.bucket, "R2_BUCKET"),
        prefix=args.prefix,
    )


if __name__ == "__main__":
    main()
