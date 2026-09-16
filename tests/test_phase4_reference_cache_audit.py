from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_video_factory.domain import VisualReference
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)
from ai_video_factory.providers.salad_ideogram import (
    build_ideogram_job_request,
    reference_caption_variants,
)
from ai_video_factory.workers.ideogram4 import (
    IDEOGRAM4_MODEL_ID,
    IDEOGRAM4_REFERENCE_TASK,
)
from scripts.audit_phase4_reference_cache import _load_references, audit_reference_cache


def _caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description=(
                "Canonical location reference. Distant rocky hills beneath a clear sky."
            ),
            style=IdeogramStylePlan(
                aesthetics="cinematic documentary",
                lighting="even neutral reference lighting with clear readable form",
                medium="cinematic documentary reference photograph",
                render_mode="photo",
                render_description="realistic reference photography",
            ),
            background="Stable physical geography and layout.",
            elements=[],
        )
    )


class FakeStorage:
    def __init__(self, objects: dict[str, StoredObject]) -> None:
        self.objects = objects
        self.downloads = 0

    def stat(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def download(self, key: str, destination: Path) -> StoredObject:
        self.downloads += 1
        raise AssertionError("metadata-only audit must not download objects")

    def upload(self, *args: Any, **kwargs: Any) -> StoredObject:
        raise AssertionError("cache audit must never upload objects")

    def ping(self) -> None:
        return None


def _request(caption: str | None = None):
    return build_ideogram_job_request(
        task_name=IDEOGRAM4_REFERENCE_TASK,
        caption=_caption() if caption is None else caption,
        model_id=IDEOGRAM4_MODEL_ID,
        width=1024,
        height=1024,
    )


def _stored(request: Any, *, artifact_sha256: str = "a" * 64) -> StoredObject:
    return StoredObject(
        key=request.output.key,
        content_type="image/png",
        size_bytes=321,
        etag="etag-1",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": artifact_sha256,
        },
    )


def test_phase4_cache_audit_reports_verified_canonical_hit_without_download() -> None:
    request = _request()
    storage = FakeStorage({request.output.key: _stored(request)})

    records = audit_reference_cache(
        [VisualReference(entity_id="location_001", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        size="1024x1024",
    )

    record = records[0]
    assert record["status"] == "hit"
    assert record["matched_variant"] == "canonical"
    assert record["job_id"] == request.job_id
    assert record["stored_job_id"] == request.job_id
    assert record["stored_request_sha256"] == request.fingerprint()
    assert record["stored_artifact_sha256"] == "a" * 64
    assert record["candidate_job_ids"]["canonical"] == request.job_id
    assert "safe_fallback" in record["candidate_job_ids"]
    assert storage.downloads == 0


def test_phase4_cache_audit_reports_fallback_hit_without_queue_submission() -> None:
    variants = reference_caption_variants(
        _caption(),
        task_name=IDEOGRAM4_REFERENCE_TASK,
    )
    fallback_request = _request(variants[1][1])
    storage = FakeStorage({fallback_request.output.key: _stored(fallback_request)})

    records = audit_reference_cache(
        [VisualReference(entity_id="location_001", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        size="1024x1024",
    )

    assert records[0]["status"] == "hit"
    assert records[0]["matched_variant"] == "safe_fallback"
    assert records[0]["job_id"] == fallback_request.job_id
    assert storage.downloads == 0


def test_phase4_cache_audit_reports_invalid_metadata_without_mutating_storage() -> None:
    request = _request()
    invalid = StoredObject(
        key=request.output.key,
        content_type="image/png",
        size_bytes=321,
        etag="etag-1",
        metadata={
            "job-id": request.job_id,
            "request-sha256": "wrong-fingerprint",
            "artifact-sha256": "a" * 64,
        },
    )
    storage = FakeStorage({request.output.key: invalid})

    records = audit_reference_cache(
        [VisualReference(entity_id="location_001", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        size="1024x1024",
    )

    assert records[0]["status"] == "invalid"
    assert "metadata does not match" in records[0]["error"]
    assert storage.downloads == 0


def test_phase4_cache_audit_loads_utf8_bom_reference_files(tmp_path: Path) -> None:
    path = tmp_path / "visual_references.json"
    payload = [{"entity_id": "location_001", "prompt": _caption()}]
    path.write_text("\ufeff" + json.dumps(payload), encoding="utf-8")

    references = _load_references(path)

    assert references == [VisualReference(entity_id="location_001", prompt=_caption())]


def test_phase4_cache_audit_script_has_no_salad_queue_client() -> None:
    script = Path("scripts/audit_phase4_reference_cache.py").read_text(encoding="utf-8")

    assert "SaladJobQueueClient" not in script
    assert "providers.salad_queue" not in script
    assert "queue.submit" not in script
    assert "Salad queue submissions=0" in script
