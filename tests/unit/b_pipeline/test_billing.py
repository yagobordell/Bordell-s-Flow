import json
from pathlib import Path
from types import SimpleNamespace

from ai_video_factory.bots.billing import ApiCostLedger


def response(
    *,
    model: str = "gpt-6-luna",
    service_tier: str = "default",
    input_tokens: int = 1000,
    cached_tokens: int = 200,
    cache_write_tokens: int = 100,
    output_tokens: int = 100,
    reasoning_tokens: int = 25,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_example",
        model=model,
        service_tier=service_tier,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            input_tokens_details=SimpleNamespace(
                cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens
            ),
            output_tokens=output_tokens,
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
    )


def test_actual_cached_and_cache_write_tokens_are_priced_once() -> None:
    ledger = ApiCostLedger()
    ledger.record("B1.1", None, response())
    ledger.record("B1.2", 2, response())
    ledger.record("B2", 2, response())

    report = ledger.report(blocks=1, run_status="completed")

    assert report["pricing_status"] == "complete"
    assert report["estimated_total_usd"] == "0.00040350"
    assert report["stages"]["B1.1"]["estimated_cost_usd"] == "0.00013450"
    assert report["stages"]["B1.2"]["estimated_cost_usd"] == "0.00013450"
    assert report["stages"]["B2"]["estimated_cost_usd"] == "0.00013450"
    assert report["requests"][0]["reasoning_tokens_included_in_output"] == 25
    assert report["requests"][1]["block_id"] == 2
    assert report["pricing_tier"] == "Standard"


def test_long_context_threshold_applies_both_input_and_output_uplifts() -> None:
    ledger = ApiCostLedger()
    ledger.record(
        "B1.1",
        None,
        response(
            input_tokens=272001,
            cached_tokens=0,
            cache_write_tokens=0,
            output_tokens=10,
            reasoning_tokens=3,
        ),
    )
    record = ledger.records[0]

    assert record["long_context_pricing"] is True
    assert record["estimated_cost_usd"] == "0.05440770"


def test_unrecognized_model_or_tier_is_not_invented_as_zero_cost() -> None:
    ledger = ApiCostLedger()
    ledger.record("B1.1", None, response(model="unknown-model"))
    ledger.record("B1.2", 1, response(service_tier="flex"))
    ledger.record("B2", 1, response())

    report = ledger.report(blocks=1, run_status="completed")

    assert report["estimated_total_usd"] is None
    assert report["pricing_status"] == "partial_or_unavailable"
    assert report["priced_subtotal_usd"] == "0.00013450"
    assert report["stages"]["B1.1"]["estimated_cost_usd"] is None


def test_missing_usage_is_reported_unknown_not_free() -> None:
    ledger = ApiCostLedger()
    ledger.record(
        "B1.1",
        None,
        SimpleNamespace(id="resp_no_usage", model="gpt-6-luna", service_tier="default"),
    )

    report = ledger.report(blocks=1, run_status="completed")

    assert report["estimated_total_usd"] is None
    assert report["requests"][0]["estimated_cost_usd"] is None
    assert "Missing" in report["requests"][0]["pricing_note"]


def test_failed_run_reports_only_known_subtotal_without_complete_total() -> None:
    ledger = ApiCostLedger()
    ledger.record("B1.1", None, response())

    report = ledger.report(blocks=None, run_status="failed")

    assert report["run_status"] == "failed"
    assert report["estimated_total_usd"] is None
    assert report["priced_subtotal_usd"] == "0.00013450"
    assert report["stages"]["B1.2"]["estimated_cost_usd"] is None


def test_invalid_or_overlapping_cache_counts_cannot_produce_negative_cost() -> None:
    ledger = ApiCostLedger()
    ledger.record(
        "B1.1", None, response(input_tokens=100, cached_tokens=80, cache_write_tokens=50)
    )

    assert ledger.records[0]["estimated_cost_usd"] is None
    assert "inconsistent" in ledger.records[0]["pricing_note"]


def test_returned_versioned_luna_model_uses_verified_luna_rates() -> None:
    ledger = ApiCostLedger()
    ledger.record("B1.1", None, response(model="gpt-6-luna-2026-09-01"))

    assert ledger.records[0]["estimated_cost_usd"] == "0.00013450"


def test_unreported_cache_write_usage_does_not_claim_complete_pricing() -> None:
    ledger = ApiCostLedger()
    reply = response()
    del reply.usage.input_tokens_details.cache_write_tokens
    ledger.record("B1.1", None, reply)

    report = ledger.report(blocks=0, run_status="completed")

    assert report["estimated_total_usd"] is None
    assert report["requests"][0]["estimated_cost_usd"] is None
    assert report["requests"][0]["cache_write_tokens"] is None


def test_fish_director_is_billed_once_across_audio_runs_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "audio/runs/first/blocks/block_1/task.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "block_id": 1, "status": "completed", "director_model": "gpt-6-luna",
        "director_response": {
            "id": "fish_resp_1", "model": "gpt-6-luna", "service_tier": "default",
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 100},
                "output_tokens": 100,
                "output_tokens_details": {"reasoning_tokens": 25},
            },
        },
    }), encoding="utf-8")
    ledger = ApiCostLedger()
    ledger.record("B1.1", None, response())
    ledger.sync_fish_director_usage(tmp_path)
    ledger.sync_fish_director_usage(tmp_path)
    report = ledger.report(blocks=0, run_status="completed")
    assert report["estimated_total_usd"] == "0.00026900"
    assert report["stages"]["Fish director"]["estimated_cost_usd"] == "0.00013450"
    assert len(report["requests"]) == 2

    resumed = ApiCostLedger()
    resumed.restore_requests(report["requests"])
    resumed.sync_fish_director_usage(tmp_path)
    after_resume = resumed.report(blocks=0, run_status="completed")
    assert after_resume["estimated_total_usd"] == report["estimated_total_usd"]
    assert len(after_resume["requests"]) == 2


def test_unreported_director_usage_is_not_treated_as_free(tmp_path: Path) -> None:
    path = tmp_path / "audio/runs/failed/blocks/block_1/task.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "block_id": 1, "status": "director_invalid", "director_model": "gpt-6-luna",
    }), encoding="utf-8")
    ledger = ApiCostLedger()
    ledger.sync_fish_director_usage(tmp_path)
    report = ledger.report(
        blocks=0, run_status="completed",
        expected_calls={"B1.1": 0, "B1.2": 0, "B2": 0},
    )
    assert report["pricing_status"] == "partial_or_unavailable"
    assert report["estimated_total_usd"] is None
    assert report["stages"]["Fish director"]["recorded_calls"] == 1
    assert report["requests"][0]["estimated_cost_usd"] is None


def test_medium_reasoning_tokens_are_already_inside_billed_output() -> None:
    ledger = ApiCostLedger()
    ledger.record("B1.1", None, response(
        input_tokens=1000, cached_tokens=0, cache_write_tokens=0,
        output_tokens=1100, reasoning_tokens=1000,
    ))
    assert ledger.records[0]["estimated_cost_usd"] == "0.00065000"
    assert ledger.records[0]["reasoning_tokens_included_in_output"] == 1000



def test_professional_v_usage_restored_from_saved_calls_once_across_resumes(tmp_path):
    for stage, rel in (
        ("V1", "V1/pass_1"),
        ("V2", "V2/pass_1/block_1"),
        ("V3", "V3/block_1"),
    ):
        path = tmp_path / rel / "api_call_1.json"
        path.parent.mkdir(parents=True)
        obj = response()
        obj.id = "resp_" + stage
        path.write_text(json.dumps({
            "stage": stage, "block_id": None if stage == "V1" else 1,
            "request_sha256": "a" * 64, "outcome": "received",
            "elapsed_seconds": 0.03,
            "response": {
                "id": obj.id, "model": obj.model, "service_tier": obj.service_tier,
                "usage": {
                    "input_tokens": obj.usage.input_tokens,
                    "output_tokens": obj.usage.output_tokens,
                    "input_tokens_details": {
                        "cached_tokens": obj.usage.input_tokens_details.cached_tokens,
                        "cache_write_tokens": obj.usage.input_tokens_details.cache_write_tokens,
                    },
                    "output_tokens_details": {
                        "reasoning_tokens": obj.usage.output_tokens_details.reasoning_tokens,
                    },
                },
            },
        }))
    ledger = ApiCostLedger()
    ledger.record("B1.1", None, response())
    # The full runner receives V usage in memory and later reconciles the
    # same checkpoint records. Both paths must price precisely once.
    already = response()
    already.id = "resp_V1"
    ledger.record("V1", None, already)
    ledger.sync_professional_voice_usage(tmp_path)
    ledger.sync_professional_voice_usage(tmp_path)
    result = ledger.report(
        blocks=0, run_status="completed",
        expected_calls={"B1.1": 1, "B1.2": 0, "B2": 0},
    )
    assert all(result["stages"][stage]["recorded_calls"] == 1
               for stage in ("V1", "V2", "V3"))
    assert all(result["stages"][stage]["estimated_cost_usd"] == "0.00013450"
               for stage in ("V1", "V2", "V3"))
    assert result["estimated_total_usd"] == "0.00053800"
    resumed = ApiCostLedger()
    resumed.restore_requests(result["requests"])
    resumed.sync_professional_voice_usage(tmp_path)
    assert resumed.report(
        blocks=0, run_status="completed",
        expected_calls={"B1.1": 1, "B1.2": 0, "B2": 0},
    )["estimated_total_usd"] == result["estimated_total_usd"]


def test_professional_v_unknown_api_response_is_unpriced_not_free(tmp_path):
    path = tmp_path / "voice_direction/V3/block_1/api_call_1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "stage": "V3", "block_id": 1, "outcome": "schema_invalid",
        "response": {
            "id": None, "model": "gpt-6-luna",
            "service_tier": "default", "usage": None,
        },
    }))
    ledger = ApiCostLedger()
    ledger.sync_professional_voice_usage(tmp_path)
    ledger.sync_professional_voice_usage(tmp_path)
    result = ledger.report(
        blocks=0, run_status="completed",
        expected_calls={"B1.1": 0, "B1.2": 0, "B2": 0},
    )
    assert result["stages"]["V3"]["recorded_calls"] == 1
    assert result["stages"]["V3"]["estimated_cost_usd"] is None
    assert result["estimated_total_usd"] is None
