"""Offline accounting of actual GPT Image API usage in the B2 run report."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.bots.billing import ApiCostLedger
from scripts.pipeline import run_b_pipeline as runner


def _usage(*, text: int = 100, image: int = 20, output: int = 1000) -> dict:
    return {
        "input_tokens": text + image,
        "input_tokens_details": {"text_tokens": text, "image_tokens": image},
        "output_tokens": output,
        "total_tokens": text + image + output,
    }


def _state(
    output: Path,
    beat: str = "1B",
    *,
    usage: dict | None = None,
    status: str = "completed",
    model: str = "gpt-image-2.5-flare",
    folder: str = "block_1",
) -> Path:
    path = output / "images" / folder / f"{beat}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": "openai-image-v1",
            "block_id": 1,
            "beat_id": beat,
            "model_id": model,
            "status": status,
            "artifact": {
                "model_id": model,
                "openai_usage": usage,
            } if status == "completed" else None,
        }),
        encoding="utf-8",
    )
    return path


def _manifest(output: Path, jobs: int, *, status: str = "completed") -> None:
    path = output / "images" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"total_generation_jobs": jobs, "status": status}),
        encoding="utf-8",
    )


def _report(ledger: ApiCostLedger) -> dict:
    return ledger.report(
        blocks=0,
        run_status="completed",
        expected_calls={"B1.1": 0, "B1.2": 0, "B2": 0, "Fish director": 0},
    )


def test_planning_and_image_usage_share_the_same_run_total(tmp_path: Path) -> None:
    _manifest(tmp_path, 1)
    _state(tmp_path, usage=_usage())
    ledger = ApiCostLedger()
    ledger.record(
        "B1.1", None, SimpleNamespace(
            id="resp_planning",
            model="gpt-6-luna",
            service_tier="default",
            usage=SimpleNamespace(
                input_tokens=1000,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=200, cache_write_tokens=100,
                ),
                output_tokens=100,
                output_tokens_details=SimpleNamespace(reasoning_tokens=25),
            ),
        ),
    )
    ledger.sync_image_usage(tmp_path)
    report = ledger.report(
        blocks=0, run_status="completed",
        expected_calls={"B1.1": 1, "B1.2": 0, "B2": 0, "Fish director": 0},
    )

    assert report["stages"]["B1.1"]["estimated_cost_usd"] == "0.00013450"
    assert report["stages"]["B2 images"]["estimated_cost_usd"] == "0.03066000"
    assert report["estimated_total_usd"] == "0.03079450"


def test_image_usage_is_priced_and_added_to_completed_run(tmp_path: Path) -> None:
    _manifest(tmp_path, 2)
    _state(tmp_path, "1B", usage=_usage())
    _state(tmp_path, "1C", usage=_usage(text=200, image=0, output=500))
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)

    assert report["pricing_status"] == "complete"
    assert report["stages"]["B2 images"]["expected_calls"] == 2
    assert report["stages"]["B2 images"]["priced_calls"] == 2
    assert report["stages"]["B2 images"]["estimated_cost_usd"] == "0.04666000"
    assert report["estimated_total_usd"] == "0.04666000"
    assert report["requests"][0]["text_input_tokens"] == 100
    assert report["requests"][0]["image_input_tokens"] == 20
    assert report["image_pricing_per_million_usd"]["image_output"] == "30.00"


def test_image_edit_reference_tokens_and_grid_are_counted_once(tmp_path: Path) -> None:
    _manifest(tmp_path, 1)
    _state(
        tmp_path, "block_1_1B", folder="grids", model="gpt-image-2.5-sunburst",
        usage=_usage(image=500),
    )
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)

    assert report["stages"]["B2 images"]["recorded_calls"] == 1
    assert report["estimated_total_usd"] == "0.03450000"
    assert report["requests"][0]["image_input_tokens"] == 500


def test_missing_usage_is_unknown_instead_of_free(tmp_path: Path) -> None:
    _manifest(tmp_path, 1)
    _state(tmp_path, usage={"input_tokens": 120, "output_tokens": 1000})
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)

    assert report["estimated_total_usd"] is None
    assert report["priced_subtotal_usd"] == "0.00000000"
    assert report["stages"]["B2 images"]["estimated_cost_usd"] is None
    assert "Missing" in report["requests"][0]["pricing_note"]


def test_unknown_post_does_not_claim_zero_cost(tmp_path: Path) -> None:
    _manifest(tmp_path, 1, status="incomplete")
    _state(tmp_path, status="direct_submitting_unknown")
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)

    assert report["pricing_status"] == "partial_or_unavailable"
    assert report["stages"]["B2 images"]["expected_calls"] == 1
    assert report["stages"]["B2 images"]["recorded_calls"] == 1
    assert report["estimated_total_usd"] is None
    assert "unknown" in report["requests"][0]["pricing_note"]


def test_incomplete_manifest_includes_only_known_subtotal(tmp_path: Path) -> None:
    _manifest(tmp_path, 2, status="incomplete")
    _state(tmp_path, usage=_usage())
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)

    assert report["stages"]["B2 images"]["expected_calls"] == 2
    assert report["stages"]["B2 images"]["recorded_calls"] == 1
    assert report["stages"]["B2 images"]["priced_subtotal_usd"] == "0.03066000"
    assert report["estimated_total_usd"] is None


def test_replaced_images_keep_historical_charges_without_resume_duplicates(
    tmp_path: Path,
) -> None:
    _manifest(tmp_path, 1)
    original = _state(tmp_path, usage=_usage())
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    earlier = _report(ledger)
    archive = tmp_path / "images" / "replaced_states" / "block_1" / "1B.legacy.json"
    archive.parent.mkdir(parents=True)
    original.replace(archive)
    _state(tmp_path, usage=_usage(text=200, image=0, output=500))

    resumed = ApiCostLedger()
    resumed.restore_requests(earlier["requests"])
    resumed.sync_image_usage(tmp_path)
    resumed.sync_image_usage(tmp_path)
    report = _report(resumed)

    assert report["stages"]["B2 images"]["expected_calls"] == 2
    assert report["stages"]["B2 images"]["recorded_calls"] == 2
    assert report["estimated_total_usd"] == "0.04666000"
    assert any("replaced_states" in record["image_state_file"] for record in report["requests"])


def test_unknown_model_and_bad_usage_are_not_silently_priced(tmp_path: Path) -> None:
    _manifest(tmp_path, 2)
    _state(tmp_path, "1B", model="unverified-image-model", usage=_usage())
    _state(tmp_path, "1C", usage={
        "input_tokens": 5,
        "input_tokens_details": {"text_tokens": 100, "image_tokens": 20},
        "output_tokens": 1000,
    })
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)

    assert report["estimated_total_usd"] is None
    assert report["stages"]["B2 images"]["priced_calls"] == 0
    assert "No verified price" in report["requests"][0]["pricing_note"]
    assert "inconsistent" in report["requests"][1]["pricing_note"]


def test_images_only_refresh_reconciles_costs_from_saved_states(tmp_path: Path) -> None:
    _manifest(tmp_path, 1)
    _state(tmp_path, usage=_usage())
    ledger = ApiCostLedger()
    report = {
        "run": {"status": "completed"},
        "api_costs": _report(ledger),
    }
    runner._refresh_audio_costs(report, tmp_path)
    runner._refresh_audio_costs(report, tmp_path)

    assert report["api_costs"]["estimated_total_usd"] == "0.03066000"
    assert len(report["api_costs"]["requests"]) == 1
    assert report["api_costs"]["stages"]["B2 images"]["priced_calls"] == 1


@pytest.mark.parametrize("status", ["direct_pending", "completed"])
def test_unsubmitted_or_missing_usage_is_not_assumed_free(
    tmp_path: Path, status: str,
) -> None:
    _manifest(tmp_path, 1, status="incomplete")
    _state(tmp_path, status=status)
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)

    assert report["estimated_total_usd"] is None
    assert report["stages"]["B2 images"]["priced_calls"] == 0


def test_professional_mixed_grid_native_edits_are_accounted_once(tmp_path: Path) -> None:
    _manifest(tmp_path, 1)
    original = _state(tmp_path, "block_1_1B", folder="grids", usage=_usage())
    manifest = tmp_path / "images" / "manifest.json"
    doc = json.loads(manifest.read_text())
    doc["native_grid_video_calls"] = 1
    manifest.write_text(json.dumps(doc))
    state = tmp_path / "images" / "professional_finished" / "block_1" / "1C-native_grid_video.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "schema_version": "professional-image-finishing-v1",
        "block_id": 1, "beat_id": "1C", "model_id": "gpt-image-2.5-flare",
        "status": "completed",
        "artifact": {"openai_usage": _usage(text=200, image=100, output=500)},
    }))
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)
    assert report["stages"]["B2 images"]["expected_calls"] == 2
    assert report["stages"]["B2 images"]["priced_calls"] == 2
    assert len(report["requests"]) == 2
    assert original.is_file()
    assert any("professional_finished" in r["image_state_file"] for r in report["requests"])


def test_professional_native_unknown_edit_is_never_counted_as_free(tmp_path: Path) -> None:
    _manifest(tmp_path, 1)
    doc_path = tmp_path / "images" / "manifest.json"
    doc = json.loads(doc_path.read_text())
    doc["native_grid_video_calls"] = 1
    doc_path.write_text(json.dumps(doc))
    _state(tmp_path, "block_1_1B", folder="grids", usage=_usage())
    state = tmp_path / "images" / "professional_finished" / "block_1" / "1C-native_grid_video.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "schema_version": "professional-image-finishing-v1",
        "block_id": 1, "beat_id": "1C", "model_id": "gpt-image-2.5-flare",
        "status": "direct_submitting_unknown",
    }))
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = _report(ledger)
    assert report["stages"]["B2 images"]["expected_calls"] == 2
    assert report["estimated_total_usd"] is None
    assert any(r["image_request_status"] == "direct_submitting_unknown"
               for r in report["requests"])
