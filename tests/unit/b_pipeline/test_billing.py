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
