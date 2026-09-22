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
from ai_video_factory.workers.flux2_klein import (
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_REFERENCE_TASK,
)
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_REFERENCE_TASK

DEFAULT_SIZE = "1536x864"
_LEGACY_CACHE_ONLY_VARIANT = "safe_fallback"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Phase 4 reference cache entries and safety fallback planning in R2 without "
            "submitting any Salad Job Queue work."
        )
    )
    parser.add_argument(
        "references_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase4" / "visual_references.json",
    )
    parser.add_argument("--model", default=settings.ideogram4_model)
    parser.add_argument("--fallback-model", default=settings.flux2_klein_model)
    parser.add_argument(
        "--primary-provider",
        choices=("flux2_klein", "ideogram4"),
        default="flux2_klein",
        help="Provider whose canonical cache is audited first.",
    )
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


def _cache_record(
    *,
    storage: R2ObjectStorage,
    request: Any,
    verify_content_sha256: bool,
) -> tuple[str, dict[str, Any]]:
    stored = storage.stat(request.output.key)
    fields: dict[str, Any] = {
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
        return "miss", fields
    fields.update(
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
        fields["error"] = str(exc)
        return "invalid", fields
    if response is None:
        return "miss", fields
    if verify_content_sha256:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "image.png"
            downloaded = storage.download(request.output.key, destination)
            digest = sha256_file(destination)
        verified = (
            downloaded.size_bytes == response.output.size_bytes
            and digest == response.output.sha256
        )
        fields["content_sha256_verified"] = verified
        if not verified:
            fields["error"] = "Downloaded content does not match cached size/SHA-256 metadata"
            return "invalid", fields
    return "hit", fields


def audit_reference_cache(
    references: list[VisualReference],
    *,
    storage: R2ObjectStorage,
    model_id: str,
    size: str,
    fallback_model_id: str = FLUX2_KLEIN_MODEL_ID,
    primary_provider: str = "ideogram4",
    verify_content_sha256: bool = False,
) -> list[dict[str, Any]]:
    width, height = parse_ideogram_size(size)
    records: list[dict[str, Any]] = []

    for reference in references:
        if primary_provider == "flux2_klein":
            flux_request = build_flux_job_request(
                task_name=FLUX2_KLEIN_REFERENCE_TASK,
                prompt=reference.prompt,
                model_id=fallback_model_id,
                width=width,
                height=height,
            )
            status, fields = _cache_record(
                storage=storage,
                request=flux_request,
                verify_content_sha256=verify_content_sha256,
            )
            records.append(
                {
                    "entity_id": reference.entity_id,
                    "status": status,
                    "provider": "flux2_klein",
                    "matched_variant": "flux2_klein" if status == "hit" else None,
                    "job_id": flux_request.job_id,
                    "object_key": flux_request.output.key,
                    "expected_request_sha256": flux_request.fingerprint(),
                    "expected_content_type": flux_request.output.content_type,
                    "candidate_job_ids": {},
                    "safety_rejected_variants": [],
                    "fallback_job_id": None,
                    "fallback_object_key": None,
                    **fields,
                }
            )
            continue

        ideogram_candidates: list[tuple[str, Any]] = []
        for variant_name, caption in reference_caption_variants(
            reference.prompt,
            task_name=IDEOGRAM4_REFERENCE_TASK,
        ):
            request = build_ideogram_job_request(
                task_name=IDEOGRAM4_REFERENCE_TASK,
                caption=caption,
                model_id=model_id,
                width=width,
                height=height,
            )
            ideogram_candidates.append((variant_name, request))

        canonical = ideogram_candidates[0][1]
        record: dict[str, Any] = {
            "entity_id": reference.entity_id,
            "status": "miss",
            "provider": "ideogram4",
            "matched_variant": None,
            "job_id": canonical.job_id,
            "object_key": canonical.output.key,
            "expected_request_sha256": canonical.fingerprint(),
            "expected_content_type": canonical.output.content_type,
            "candidate_job_ids": {
                variant_name: request.job_id for variant_name, request in ideogram_candidates
            },
            "safety_rejected_variants": [],
            "fallback_job_id": None,
            "fallback_object_key": None,
        }
        record.update(
            {
                "stored_job_id": None,
                "stored_request_sha256": None,
                "stored_artifact_sha256": None,
                "stored_content_type": None,
                "stored_size_bytes": None,
                "stored_etag": None,
                "content_sha256_verified": None,
                "error": None,
            }
        )

        for variant_name, request in ideogram_candidates:
            status, fields = _cache_record(
                storage=storage,
                request=request,
                verify_content_sha256=verify_content_sha256,
            )
            if status == "invalid":
                record.update(fields)
                record.update(
                    status="invalid",
                    matched_variant=variant_name,
                    job_id=request.job_id,
                    object_key=request.output.key,
                    expected_request_sha256=request.fingerprint(),
                )
                break
            if status == "hit":
                record.update(fields)
                record.update(
                    status="hit",
                    matched_variant=variant_name,
                    job_id=request.job_id,
                    object_key=request.output.key,
                    expected_request_sha256=request.fingerprint(),
                )
                break
        if record["status"] in {"hit", "invalid"}:
            records.append(record)
            continue

        executable = [
            (variant_name, request)
            for variant_name, request in ideogram_candidates
            if variant_name != _LEGACY_CACHE_ONLY_VARIANT
        ]
        rejected = [
            variant_name
            for variant_name, request in executable
            if find_safety_rejection(storage, request) is not None
        ]
        record["safety_rejected_variants"] = rejected
        if len(rejected) != len(executable):
            records.append(record)
            continue

        fallback_request = build_flux_job_request(
            task_name=FLUX2_KLEIN_REFERENCE_TASK,
            prompt=reference.prompt,
            model_id=fallback_model_id,
            width=width,
            height=height,
        )
        record.update(
            provider="flux2_klein",
            status="safety_blocked",
            matched_variant="flux2_klein",
            fallback_job_id=fallback_request.job_id,
            fallback_object_key=fallback_request.output.key,
            job_id=fallback_request.job_id,
            object_key=fallback_request.output.key,
            expected_request_sha256=fallback_request.fingerprint(),
        )
        fallback_status, fields = _cache_record(
            storage=storage,
            request=fallback_request,
            verify_content_sha256=verify_content_sha256,
        )
        record.update(fields)
        if fallback_status == "hit":
            record["status"] = "hit"
        elif fallback_status == "invalid":
            record["status"] = "invalid"
        records.append(record)

    return records


def _print_report(records: list[dict[str, Any]], *, metadata_only: bool) -> None:
    for record in records:
        print(
            "{status:<14} entity={entity} provider={provider} variant={variant} job={job}".format(
                status=str(record["status"]).upper(),
                entity=record["entity_id"],
                provider=record["provider"],
                variant=record["matched_variant"],
                job=record["job_id"],
            )
        )
        if record["safety_rejected_variants"]:
            print("               ideogram_safety=" + ",".join(record["safety_rejected_variants"]))
        if record["error"]:
            print(f"               error={record['error']}")

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
    storage = R2ObjectStorage.create(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
    )
    records = audit_reference_cache(
        _load_references(args.references_file),
        storage=storage,
        model_id=args.model,
        fallback_model_id=args.fallback_model,
        primary_provider=args.primary_provider,
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
