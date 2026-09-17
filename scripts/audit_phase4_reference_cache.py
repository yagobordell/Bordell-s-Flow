from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.domain import VisualReference
from ai_video_factory.inference.storage import R2ObjectStorage, sha256_file
from ai_video_factory.providers.ideogram_rejections import find_safety_rejection
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.providers.salad_flux import build_flux_job_request
from ai_video_factory.providers.salad_ideogram import (
    build_ideogram_job_request,
    parse_ideogram_size,
    reference_caption_variants,
)
from ai_video_factory.workers.flux_schnell import (
    FLUX_SCHNELL_MODEL_ID,
    FLUX_SCHNELL_REFERENCE_TASK,
)
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_REFERENCE_TASK

DEFAULT_SIZE = "1024x1024"
_LEGACY_CACHE_ONLY_VARIANT = "safe_fallback"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Phase 4 image caches and Ideogram negative safety cache without submitting "
            "any Salad queue work."
        )
    )
    parser.add_argument(
        "references_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase4" / "visual_references.json",
    )
    parser.add_argument("--model", default=settings.ideogram4_model)
    parser.add_argument("--flux-model", default=settings.flux_schnell_model)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument("--verify-content-sha256", action="store_true")
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def _load_references(path: Path) -> list[VisualReference]:
    if not path.is_file():
        raise SystemExit(f"Visual references file not found: {path}")
    raw: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise SystemExit("Visual references file must contain a JSON array.")
    references = [VisualReference.model_validate(item) for item in raw]
    entity_ids = [reference.entity_id for reference in references]
    if len(entity_ids) != len(set(entity_ids)):
        raise SystemExit("Visual references must have unique entity IDs.")
    return references


def _verify_cached_content(
    storage: R2ObjectStorage,
    request: Any,
    response: Any,
) -> tuple[bool, str | None]:
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "image.png"
        downloaded = storage.download(request.output.key, destination)
        digest = sha256_file(destination)
    valid = (
        downloaded.size_bytes == response.output.size_bytes
        and digest == response.output.sha256
    )
    error = None if valid else "Downloaded content does not match cached size/SHA-256 metadata"
    return valid, error


def audit_reference_cache(
    references: list[VisualReference],
    *,
    storage: R2ObjectStorage,
    model_id: str,
    flux_model_id: str = FLUX_SCHNELL_MODEL_ID,
    size: str,
    verify_content_sha256: bool = False,
) -> list[dict[str, Any]]:
    width, height = parse_ideogram_size(size)
    records: list[dict[str, Any]] = []

    for reference in references:
        candidates: list[tuple[str, Any]] = []
        for variant_name, caption in reference_caption_variants(
            reference.prompt,
            task_name=IDEOGRAM4_REFERENCE_TASK,
        ):
            candidates.append(
                (
                    variant_name,
                    build_ideogram_job_request(
                        task_name=IDEOGRAM4_REFERENCE_TASK,
                        caption=caption,
                        model_id=model_id,
                        width=width,
                        height=height,
                    ),
                )
            )

        canonical_request = candidates[0][1]
        flux_request = build_flux_job_request(
            task_name=FLUX_SCHNELL_REFERENCE_TASK,
            caption=reference.prompt,
            model_id=flux_model_id,
            width=width,
            height=height,
        )
        record: dict[str, Any] = {
            "entity_id": reference.entity_id,
            "status": "miss",
            "primary_status": "miss",
            "matched_provider": None,
            "matched_variant": None,
            "job_id": canonical_request.job_id,
            "object_key": canonical_request.output.key,
            "expected_request_sha256": canonical_request.fingerprint(),
            "candidate_job_ids": {
                variant_name: request.job_id for variant_name, request in candidates
            },
            "safety_rejections": {},
            "fallback_job_id": flux_request.job_id,
            "fallback_object_key": flux_request.output.key,
            "fallback_status": "miss",
            "stored_job_id": None,
            "stored_request_sha256": None,
            "stored_artifact_sha256": None,
            "stored_content_type": None,
            "stored_size_bytes": None,
            "stored_etag": None,
            "content_sha256_verified": None,
            "error": None,
        }

        for variant_name, request in candidates:
            stored = storage.stat(request.output.key)
            if stored is None:
                continue
            try:
                response = cached_inference_response(storage, request)
            except RuntimeError as exc:
                record["status"] = "invalid"
                record["primary_status"] = "invalid"
                record["error"] = str(exc)
                break
            if response is None:
                continue
            record.update(
                {
                    "status": "hit",
                    "primary_status": "hit",
                    "matched_provider": "ideogram4",
                    "matched_variant": variant_name,
                    "job_id": request.job_id,
                    "object_key": request.output.key,
                    "expected_request_sha256": request.fingerprint(),
                    "stored_job_id": stored.metadata.get("job-id"),
                    "stored_request_sha256": stored.metadata.get("request-sha256"),
                    "stored_artifact_sha256": stored.metadata.get("artifact-sha256"),
                    "stored_content_type": stored.content_type,
                    "stored_size_bytes": stored.size_bytes,
                    "stored_etag": stored.etag,
                }
            )
            if verify_content_sha256:
                valid, error = _verify_cached_content(storage, request, response)
                record["content_sha256_verified"] = valid
                if not valid:
                    record["status"] = "invalid"
                    record["primary_status"] = "invalid"
                    record["error"] = error
            break

        if record["status"] in {"hit", "invalid"}:
            records.append(record)
            continue

        executable = [
            (variant_name, request)
            for variant_name, request in candidates
            if variant_name != _LEGACY_CACHE_ONLY_VARIANT
        ]
        for variant_name, request in executable:
            rejection = find_safety_rejection(storage, request)
            if rejection is not None:
                record["safety_rejections"][variant_name] = {
                    "detail": rejection.detail,
                    "job_id": request.job_id,
                    "transport_job_id": rejection.transport_job_id,
                }

        if len(record["safety_rejections"]) != len(executable):
            records.append(record)
            continue

        record["primary_status"] = "safety_blocked"
        record["status"] = "safety_blocked"
        fallback_stored = storage.stat(flux_request.output.key)
        if fallback_stored is None:
            records.append(record)
            continue
        try:
            fallback_response = cached_inference_response(storage, flux_request)
        except RuntimeError as exc:
            record["status"] = "invalid"
            record["fallback_status"] = "invalid"
            record["error"] = str(exc)
            records.append(record)
            continue
        if fallback_response is None:
            records.append(record)
            continue

        record.update(
            {
                "status": "hit",
                "fallback_status": "hit",
                "matched_provider": "flux_schnell",
                "matched_variant": "safety_fallback",
                "job_id": flux_request.job_id,
                "object_key": flux_request.output.key,
                "expected_request_sha256": flux_request.fingerprint(),
                "stored_job_id": fallback_stored.metadata.get("job-id"),
                "stored_request_sha256": fallback_stored.metadata.get("request-sha256"),
                "stored_artifact_sha256": fallback_stored.metadata.get("artifact-sha256"),
                "stored_content_type": fallback_stored.content_type,
                "stored_size_bytes": fallback_stored.size_bytes,
                "stored_etag": fallback_stored.etag,
            }
        )
        if verify_content_sha256:
            valid, error = _verify_cached_content(storage, flux_request, fallback_response)
            record["content_sha256_verified"] = valid
            if not valid:
                record["status"] = "invalid"
                record["fallback_status"] = "invalid"
                record["error"] = error
        records.append(record)

    return records


def _print_report(records: list[dict[str, Any]], *, metadata_only: bool) -> None:
    for record in records:
        print(
            "{status:<14} entity={entity} provider={provider} variant={variant} job={job}".format(
                status=str(record["status"]).upper(),
                entity=record["entity_id"],
                provider=record["matched_provider"],
                variant=record["matched_variant"],
                job=record["job_id"],
            )
        )
        if record["primary_status"] == "safety_blocked":
            print(
                "        primary=safety_blocked flux_fallback={fallback} fallback_job={job}".format(
                    fallback=record["fallback_status"],
                    job=record["fallback_job_id"],
                )
            )
        if record["error"]:
            print(f"        error={record['error']}")

    hits = sum(record["status"] == "hit" for record in records)
    misses = sum(record["status"] == "miss" for record in records)
    safety_blocked = sum(record["status"] == "safety_blocked" for record in records)
    invalid = sum(record["status"] == "invalid" for record in records)
    mode = "R2 HEAD metadata only" if metadata_only else "R2 HEAD + content SHA-256 verification"
    print(
        f"Summary: hits={hits} misses={misses} safety_blocked={safety_blocked} "
        f"invalid={invalid} total={len(records)}; mode={mode}; Salad queue submissions=0"
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
        flux_model_id=args.flux_model,
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
