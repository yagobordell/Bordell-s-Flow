"""Per-response USD cost accounting for the standalone GPT-6 Luna planning pipeline.

This estimates API charges from returned Responses usage, not from narration word
counts or the length of the visible JSON. It is not an OpenAI invoice.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

PRICING_URL = "https://developers.openai.com/api/docs/pricing"
PRICING_AS_OF = "2026-09-26"
_PER_MILLION = Decimal("1000000")
_STANDARD_LUNA = {
    "ordinary_input": Decimal("0.10"),
    "cached_input": Decimal("0.01"),
    "cache_write": Decimal("0.125"),
    "output": Decimal("0.50"),
}
_STAGES = ("B1.1", "B1.2", "B2")


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _tokens(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usd(value: Decimal) -> str:
    # Eight fractional digits retain even a single cached token at Luna prices.
    return f"{value:.8f}"


class ApiCostLedger:
    """Record actual per-call usage and produce a conservative pricing breakdown."""

    def __init__(self) -> None:
        self._records: list[dict[str, object]] = []

    @property
    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(self._records)

    def record(self, stage: str, block_id: int | None, response: object) -> None:
        if stage not in _STAGES:
            raise ValueError(f"Unknown billing stage: {stage}")

        usage = _field(response, "usage")
        model = _field(response, "model")
        service_tier = _field(response, "service_tier")
        details = _field(usage, "input_tokens_details")
        output_details = _field(usage, "output_tokens_details")
        input_tokens = _tokens(_field(usage, "input_tokens"))
        output_tokens = _tokens(_field(usage, "output_tokens"))
        cached_tokens = _tokens(_field(details, "cached_tokens"))
        cache_write_tokens = _tokens(_field(details, "cache_write_tokens"))
        reasoning_tokens = _tokens(_field(output_details, "reasoning_tokens", 0))

        record: dict[str, object] = {
            "stage": stage,
            "block_id": block_id,
            "response_id": _field(response, "id"),
            "model": model,
            "service_tier": service_tier,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens_included_in_output": reasoning_tokens,
            "estimated_cost_usd": None,
            "pricing_note": None,
        }
        if (
            input_tokens is None
            or output_tokens is None
            or cached_tokens is None
            or cache_write_tokens is None
            or reasoning_tokens is None
            or cached_tokens + cache_write_tokens > input_tokens
            or reasoning_tokens > output_tokens
        ):
            record["pricing_note"] = "Missing or inconsistent API token usage"
        elif not isinstance(model, str) or not (
            model == "gpt-6-luna" or model.startswith("gpt-6-luna-")
        ):
            record["pricing_note"] = "No verified price for returned API model"
        elif service_tier not in (None, "default", "standard"):
            record["pricing_note"] = "No verified Standard price for returned service tier"
        else:
            ordinary = input_tokens - cached_tokens - cache_write_tokens
            long_context = input_tokens > 272_000
            input_multiplier = Decimal(2 if long_context else 1)
            output_multiplier = Decimal("1.5") if long_context else Decimal(1)
            amount = (
                Decimal(ordinary) * _STANDARD_LUNA["ordinary_input"] * input_multiplier
                + Decimal(cached_tokens) * _STANDARD_LUNA["cached_input"] * input_multiplier
                + Decimal(cache_write_tokens) * _STANDARD_LUNA["cache_write"] * input_multiplier
                + Decimal(output_tokens) * _STANDARD_LUNA["output"] * output_multiplier
            ) / _PER_MILLION
            record["estimated_cost_usd"] = _usd(amount)
            record["long_context_pricing"] = long_context
            if service_tier is None:
                record["pricing_note"] = (
                    "Service tier absent from response; Standard assumed from runner request"
                )

        self._records.append(record)

    def report(
        self,
        *,
        blocks: int | None,
        run_status: Literal["running", "completed", "failed"],
    ) -> dict[str, object]:
        ordered = sorted(
            self._records,
            key=lambda item: (_STAGES.index(str(item["stage"])), item["block_id"] or 0),
        )
        expected = {"B1.1": 1, "B1.2": blocks, "B2": blocks}
        stages: dict[str, dict[str, object]] = {}
        subtotal = Decimal(0)
        all_priced = True

        for stage in _STAGES:
            entries = [item for item in ordered if item["stage"] == stage]
            known = [item for item in entries if item["estimated_cost_usd"] is not None]
            stage_subtotal = sum(
                (Decimal(str(item["estimated_cost_usd"])) for item in known), Decimal(0)
            )
            subtotal += stage_subtotal
            complete_stage = (
                expected[stage] is not None
                and len(entries) == expected[stage]
                and len(known) == len(entries)
            )
            all_priced = all_priced and complete_stage
            stages[stage] = {
                "expected_calls": expected[stage],
                "recorded_calls": len(entries),
                "priced_calls": len(known),
                "estimated_cost_usd": _usd(stage_subtotal) if complete_stage else None,
                "priced_subtotal_usd": _usd(stage_subtotal),
            }

        complete = run_status == "completed" and all_priced
        return {
            "currency": "USD",
            "kind": "estimated_api_token_cost_not_invoice",
            "run_status": run_status,
            "pricing_status": "complete" if complete else "partial_or_unavailable",
            "pricing_model": "gpt-6-luna",
            "pricing_tier": "Standard",
            "pricing_as_of": PRICING_AS_OF,
            "pricing_source": PRICING_URL,
            "pricing_per_million_usd": {
                key: str(value) for key, value in _STANDARD_LUNA.items()
            },
            "estimated_total_usd": _usd(subtotal) if complete else None,
            "priced_subtotal_usd": _usd(subtotal),
            "stages": stages,
            "requests": ordered,
        }
