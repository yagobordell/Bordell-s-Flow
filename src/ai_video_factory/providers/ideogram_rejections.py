from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.ports import ObjectStorage

_SAFETY_REJECTION_KIND = "ideogram_safety"
_SINGLE_IMAGE_SAFETY_DETAIL = "Ideogram 4 safety filter blocked generated image"
_LEGACY_SAFETY_DETAIL = "Ideogram 4 safety filter blocked all deterministic caption variants"
_SAFETY_DETAILS = frozenset({_SINGLE_IMAGE_SAFETY_DETAIL, _LEGACY_SAFETY_DETAIL})


@dataclass(frozen=True, slots=True)
class SafetyRejection:
    detail: str
    transport_job_id: str | None


# These application jobs were paid and confirmed safety-rejected before rejection caching
# existed. Keeping them here prevents an exact replay during migration to the R2 cache.
# The tuple is (request SHA-256 or None when the job ID is sufficient, transport job ID).
_HISTORICAL_SAFETY_REJECTIONS: dict[str, tuple[str | None, str | None]] = {
    "ideogram-reference-6927a43b3213ae83dbf86a52c8722525": (None, None),
    "ideogram-reference-e6a0d2f5296bab78dce70cfd30460fa6": (
        "51cd84a1f9c48ae3ed2f0f51f912a8e7cc9ad734cfabf942eec048b10c06c79d",
        "f322302e-658c-40e5-9f62-4a2d708d9e80",
    ),
}


def is_safety_rejection_detail(detail: str) -> bool:
    return detail in _SAFETY_DETAILS


def safety_rejection_key(job_id: str) -> str:
    return f"jobs/{job_id}/rejections/safety.json"


def find_safety_rejection(
    storage: ObjectStorage,
    request: InferenceJobRequest,
) -> SafetyRejection | None:
    historical = _HISTORICAL_SAFETY_REJECTIONS.get(request.job_id)
    if historical is not None:
        historical_sha, transport_job_id = historical
        if historical_sha is None or historical_sha == request.fingerprint():
            return SafetyRejection(
                detail=_LEGACY_SAFETY_DETAIL,
                transport_job_id=transport_job_id,
            )

    stored = storage.stat(safety_rejection_key(request.job_id))
    if stored is None:
        return None
    metadata = stored.metadata
    if not (
        metadata.get("rejection-kind") == _SAFETY_REJECTION_KIND
        and metadata.get("job-id") == request.job_id
        and metadata.get("request-sha256") == request.fingerprint()
    ):
        return None
    return SafetyRejection(
        detail=metadata.get("rejection-detail", _SINGLE_IMAGE_SAFETY_DETAIL),
        transport_job_id=metadata.get("transport-job-id"),
    )


def known_safety_rejection(storage: ObjectStorage, request: InferenceJobRequest) -> bool:
    return find_safety_rejection(storage, request) is not None


def record_safety_rejection(
    storage: ObjectStorage,
    request: InferenceJobRequest,
    *,
    detail: str,
    transport_job_id: str | None,
) -> None:
    if not is_safety_rejection_detail(detail):
        raise ValueError("Only confirmed Ideogram safety rejections may be cached")

    payload = {
        "job_id": request.job_id,
        "request_sha256": request.fingerprint(),
        "rejection_kind": _SAFETY_REJECTION_KIND,
        "detail": detail,
        "transport_job_id": transport_job_id,
    }
    metadata = {
        "job-id": request.job_id,
        "request-sha256": request.fingerprint(),
        "rejection-kind": _SAFETY_REJECTION_KIND,
        "rejection-detail": detail,
        "detail-sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
    }
    if transport_job_id:
        metadata["transport-job-id"] = transport_job_id

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "safety.json"
        source.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        storage.upload(
            source,
            safety_rejection_key(request.job_id),
            content_type="application/json",
            metadata=metadata,
        )
