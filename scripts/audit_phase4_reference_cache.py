from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.domain import VisualReference
from ai_video_factory.inference.storage import R2ObjectStorage, sha256_file
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.providers.salad_ideogram import (
    build_ideogram_job_request,
    parse_ideogram_size,
)
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_REFERENCE_TASK

DEFAULT_SIZE = "1024x1024"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Phase 4 Ideogram reference cache entries in R2 without submitting any "
            "Salad Job Queue work."
        )
    )
    parser.add_argument(
        "references_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase4" / "visual_references.json",
        help="Phase 4 visual_references.json file.",
    )
    parser.add_argument(
        "--model",
        default=settings.ideogram4_model,
        help="Ideogram model ID used to derive deterministic application job IDs.",
    )
    parser.add_argument(
        "--size",
        default=DEFAULT_SIZE,
        help="Reference image size used by production, for example 1024x1024.",
    )
    parser.add_argument(
        "--verify-content-sha256",
        action="store_true",
        help=(
            "Download cache hits from R2 and recompute SHA-256. This is still read-only and "
            "never contacts Salad, but transfers object data instead of HEAD metadata only."
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for the complete machine-readable audit report.",
    )
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def _load_references(path: Path) -> list[VisualReference]:
    if not path.is_file():
        raise SystemExit(f"Visual references file not found: {path}")
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("Visual references file must contain a JSON array.")
    references = [VisualReference.model_validate(item) for item in raw]
    entity_ids = [reference.entity_id for reference in references]
    if len(entity_ids) != len(set(entity_ids)):
        raise SystemExit("Visual references must have unique entity IDs.")
    return references


def audit_reference_cache(
    references: list[VisualReference],
    *,
    storage: R2ObjectStorage,
    model_id: str,
    size: str,
    verify_content_sha256: bool = False,
) -> list[dict[str, Any]]:
    """Inspect deterministic Phase 4 cache entries without creating queue work."""

    width, height = parse_ideogram_size(size)
    records: list[dict[str, Any]] = []

    for reference in references:
        request = build_ideogram_job_request(
            task_name=IDEOGRAM4_REFERENCE_TASK,
            caption=reference.prompt,
            model_id=model_id,
            width=width,
            height=height,
        )
        stored = storage.stat(request.output.key)
        record: dict[str, Any] = {
            "entity_id": reference.entity_id,
            "status": "miss",
            "job_id": request.job_id,
            "object_key": request.output.key,
            "expected_request_sha256": request.fingerprint(),
            "expected_content_type": request.output.content_type,
            "stored_job_id": None,
            "stored_request_sha256": None,
            "stored_artifact_sha256": None,
            "stored_content_type": None,
            "stored_size_bytes": None,
            "stored_etag": None,
            "content_sha256_verified": None,
            "error": None,
        }
        if stored is None:
            records.append(record)
            continue

        record.update(
            {
                "stored_job_id": stored.metadata.get("job-id"),
                "stored_request_sha256": stored.metadata.get("request-sha256"),
                "stored_artifact_sha256": stored.metadata.get("artifact-sha256"),
                "stored_content_type": stored.content_type,
                "stored_size_bytes": stored.size_bytes,
                "stored_etag": stored.etag,
            }
        )
        try:
            response = cached_inference_response(storage, request)
        except RuntimeError as exc:
            record["status"] = "invalid"
            record["error"] = str(exc)
            records.append(record)
            continue

        if response is None:  # pragma: no cover - defensive against an object disappearing
            record["status"] = "miss"
            record["error"] = "Object disappeared between R2 metadata reads"
            records.append(record)
            continue

        record["status"] = "hit"
        if verify_content_sha256:
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "image.png"
                downloaded = storage.download(request.output.key, destination)
                digest = sha256_file(destination)
            record["content_sha256_verified"] = (
                downloaded.size_bytes == response.output.size_bytes
                and digest == response.output.sha256
            )
            if not record["content_sha256_verified"]:
                record["status"] = "invalid"
                record["error"] = "Downloaded content does not match cached size/SHA-256 metadata"

        records.append(record)

    return records


def _print_report(records: list[dict[str, Any]], *, metadata_only: bool) -> None:
    for record in records:
        print(
            "{status:<7} entity={entity} job={job} size={size} content_type={content_type} "
            "request_sha256={request_sha} artifact_sha256={artifact_sha}".format(
                status=str(record["status"]).upper(),
                entity=record["entity_id"],
                job=record["job_id"],
                size=record["stored_size_bytes"],
                content_type=record["stored_content_type"],
                request_sha=record["stored_request_sha256"],
                artifact_sha=record["stored_artifact_sha256"],
            )
        )
        if record["error"]:
            print(f"        error={record['error']}")

    hits = sum(record["status"] == "hit" for record in records)
    misses = sum(record["status"] == "miss" for record in records)
    invalid = sum(record["status"] == "invalid" for record in records)
    mode = "R2 HEAD metadata only" if metadata_only else "R2 HEAD + content SHA-256 verification"
    print(
        f"Summary: hits={hits} misses={misses} invalid={invalid} total={len(records)}; "
        f"mode={mode}; Salad queue submissions=0"
    )


def main() -> None:
    args = parse_args()
    references = _load_references(args.references_file)
    storage = R2ObjectStorage.create(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting(
            "R2_SECRET_ACCESS_KEY",
            settings.r2_secret_access_key,
        ),
    )
    records = audit_reference_cache(
        references,
        storage=storage,
        model_id=args.model,
        size=args.size,
        verify_content_sha256=args.verify_content_sha256,
    )
    _print_report(records, metadata_only=not args.verify_content_sha256)

    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Audit JSON: {args.json_output.resolve()}")

    if any(record["status"] == "invalid" for record in records):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
