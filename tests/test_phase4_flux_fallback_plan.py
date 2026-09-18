from pathlib import Path
from typing import Any

from ai_video_factory.domain import VisualReference
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)
from ai_video_factory.providers.ideogram_rejections import safety_rejection_key
from ai_video_factory.providers.salad_flux import build_flux_job_request
from ai_video_factory.providers.salad_ideogram import (
    build_ideogram_job_request,
    reference_caption_variants,
)
from ai_video_factory.workers.flux2_klein import (
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_REFERENCE_TASK,
)
from ai_video_factory.workers.ideogram4 import (
    IDEOGRAM4_MODEL_ID,
    IDEOGRAM4_REFERENCE_TASK,
)
from scripts.audit_phase4_reference_cache import audit_reference_cache


class FakeStorage:
    def __init__(self, objects: dict[str, StoredObject]) -> None:
        self.objects = objects

    def stat(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def download(self, key: str, destination: Path) -> StoredObject:
        raise AssertionError("metadata-only planner must not download objects")

    def upload(self, *args: Any, **kwargs: Any) -> StoredObject:
        raise AssertionError("planner must never upload objects")

    def ping(self) -> None:
        return None


def _caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description=(
                "Canonical location reference. A distant desert mountain range."
            ),
            style=IdeogramStylePlan(
                aesthetics="cinematic documentary",
                lighting="neutral daylight",
                medium="reference photograph",
                render_mode="photo",
                render_description="realistic reference photography",
            ),
            background="Rugged layered rock ridges along a desert horizon.",
            elements=[],
        )
    )


def _safety_object(request: Any) -> StoredObject:
    key = safety_rejection_key(request.job_id)
    return StoredObject(
        key=key,
        content_type="application/json",
        size_bytes=1,
        etag="safety-etag",
        metadata={
            "rejection-kind": "ideogram_safety",
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "rejection-detail": "Ideogram 4 safety filter blocked generated image",
        },
    )


def _output_object(request: Any) -> StoredObject:
    return StoredObject(
        key=request.output.key,
        content_type="image/png",
        size_bytes=321,
        etag="image-etag",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": "a" * 64,
        },
    )


def _blocked_storage(*, include_flux_output: bool) -> tuple[FakeStorage, Any]:
    prompt = _caption()
    objects: dict[str, StoredObject] = {}
    for variant_name, variant_caption in reference_caption_variants(
        prompt,
        task_name=IDEOGRAM4_REFERENCE_TASK,
    ):
        if variant_name == "safe_fallback":
            continue
        request = build_ideogram_job_request(
            task_name=IDEOGRAM4_REFERENCE_TASK,
            caption=variant_caption,
            model_id=IDEOGRAM4_MODEL_ID,
            width=1024,
            height=1024,
        )
        objects[safety_rejection_key(request.job_id)] = _safety_object(request)

    flux_request = build_flux_job_request(
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
        prompt=prompt,
        model_id=FLUX2_KLEIN_MODEL_ID,
        width=1024,
        height=1024,
    )
    if include_flux_output:
        objects[flux_request.output.key] = _output_object(flux_request)
    return FakeStorage(objects), flux_request


def test_phase4_planner_routes_fully_safety_blocked_reference_to_flux() -> None:
    storage, flux_request = _blocked_storage(include_flux_output=False)

    records = audit_reference_cache(
        [VisualReference(entity_id="location_004", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        size="1024x1024",
    )

    record = records[0]
    assert record["status"] == "safety_blocked"
    assert record["provider"] == "flux2_klein"
    assert record["matched_variant"] == "flux2_klein"
    assert record["fallback_job_id"] == flux_request.job_id
    assert set(record["safety_rejected_variants"]) == {
        "canonical",
        "safe_simplified",
        "safe_minimal_art",
    }


def test_phase4_planner_replays_cached_flux_fallback_without_gpu_work() -> None:
    storage, flux_request = _blocked_storage(include_flux_output=True)

    records = audit_reference_cache(
        [VisualReference(entity_id="location_004", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        size="1024x1024",
    )

    record = records[0]
    assert record["status"] == "hit"
    assert record["provider"] == "flux2_klein"
    assert record["job_id"] == flux_request.job_id


def test_phase4_runner_honors_configured_flux2_model() -> None:
    script = Path("scripts/run_phase4_assets.py").read_text(encoding="utf-8")
    assert "fallback_model=args.fallback_model" in script
