"""Per-response USD cost accounting for the standalone GPT-6 Luna planning pipeline.

This estimates API charges from returned Responses usage, not from narration word
counts or the length of the visible JSON. It is not an OpenAI invoice.
"""

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

PRICING_URL = "https://developers.openai.com/api/docs/pricing"
PRICING_AS_OF = "2026-09-29"
_PER_MILLION = Decimal("1000000")
_STANDARD_LUNA = {
    "ordinary_input": Decimal("0.10"),
    "cached_input": Decimal("0.01"),
    "cache_write": Decimal("0.125"),
    "output": Decimal("0.50"),
}
_IMAGE_STAGE = "B2 images"
_IMAGE_MODELS = {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}
_IMAGE_PRICING = {
    "text_input": Decimal("5.00"),
    "image_input": Decimal("8.00"),
    "image_output": Decimal("30.00"),
}
_STAGES = (
    "B1.1", "CTA", "CTA Ebook", "CTA Motion", "B1.2", "B2",
    "V1", "V2", "V3", "Fish director", _IMAGE_STAGE, "CTA image",
)


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
        self._expected_image_calls = 0

    @property
    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(self._records)

    def record(self, stage: str, block_id: int | None, response: object) -> None:
        if stage not in _STAGES or stage in {_IMAGE_STAGE, "CTA image"}:
            raise ValueError(f"Unknown text billing stage: {stage}")

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

    def sync_professional_voice_usage(self, output: Path) -> None:
        """Reconcile all paid V calls from durable checkpoints without double billing.

        Prior reports without new checkpoints retain their original V records.
        A missing/unknown API response stays unpriced rather than becoming $0.
        """
        states: list[Path] = []
        for root in (output / "voice_direction", output):
            for stage in ("V1", "V2", "V3"):
                stage_root = root / stage
                if stage_root.is_symlink():
                    raise ValueError("Linked professional voice billing directory")
                states.extend(sorted(stage_root.rglob("api_call_*.json")))
        seen = {
            (item["stage"], item.get("response_id"), item.get("source_state_file"))
            for item in self._records if item["stage"] in {"V1", "V2", "V3"}
        }
        response_ids = {
            (item["stage"], item.get("response_id"))
            for item in self._records
            if item["stage"] in {"V1", "V2", "V3"} and item.get("response_id") is not None
        }
        for path in states:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError(f"Linked professional voice billing state: {path}")
            item = json.loads(path.read_text(encoding="utf-8"))
            stage, block_id, response = (
                item.get("stage"), item.get("block_id"), item.get("response"),
            )
            if (stage not in {"V1", "V2", "V3"} or not isinstance(response, dict)
                or (block_id is not None and (type(block_id) is not int or block_id < 1))):
                raise ValueError(f"Malformed professional voice API call: {path}")
            relative = path.relative_to(output).as_posix()
            response_id = response.get("id")
            if ((stage, response_id, relative) in seen
                or (response_id is not None and (stage, response_id) in response_ids)):
                continue
            self.record(stage, block_id, response)
            self._records[-1]["source_state_file"] = relative
            seen.add((stage, response_id, relative))
            if response_id is not None:
                response_ids.add((stage, response_id))

    def sync_fish_director_usage(self, output: Path) -> None:
        """Reconcile durable Fish director responses without double-counting resumes."""
        runs = output / "audio" / "runs"
        if not runs.exists():
            return
        if runs.is_symlink() or not runs.is_dir():
            raise ValueError("Fish audio runs directory is invalid or linked")
        for path in sorted(runs.glob("*/blocks/block_*/task.json")):
            # Professional Gemini blocks are not Fish directors; their V-stage
            # Responses usage is recorded separately as V1/V2/V3.
            if path.parent.parent.parent.name.startswith("gemini-"):
                continue
            parents = (path, path.parent, path.parent.parent, path.parent.parent.parent)
            if any(parent.is_symlink() for parent in parents):
                raise ValueError(f"Refusing linked Fish director accounting state: {path}")
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError(f"Invalid Fish director accounting state: {path}")
            source = path.relative_to(output).as_posix()
            history = state.get("director_responses")
            if history is not None:
                if not isinstance(history, list):
                    raise ValueError(f"Malformed saved Fish director history: {path}")
                # Rebuild this block's charges from durable individual attempts.
                # The prefix also removes legacy single-response entries.
                self._records = [
                    item for item in self._records
                    if item.get("director_state_file") != source
                    and not str(item.get("director_state_file", "")).startswith(source + "#")
                ]
                for entry in history:
                    if not isinstance(entry, dict) or not isinstance(entry.get("attempt"), int):
                        raise ValueError(f"Malformed Fish director history entry: {path}")
                    response = {
                        "id": entry.get("id"),
                        "model": entry.get("model") or state.get("director_model"),
                        "service_tier": entry.get("service_tier") or "default",
                        "usage": entry.get("usage"),
                    }
                    self.record("Fish director", state.get("block_id"), response)
                    self._records[-1]["director_state_file"] = (
                        f"{source}#{entry['attempt']}"
                    )
                continue
            response = state.get("director_response")
            if response is not None and not isinstance(response, dict):
                raise ValueError(f"Malformed saved Fish director response: {path}")
            if response is None:
                if state.get("director_usage") is None and state.get("status") not in {
                    "completed", "director_completed", "director_invalid", "director_unknown",
                }:
                    continue
                response = {
                    "id": state.get("director_response_id"),
                    "model": state.get("director_model"),
                    "service_tier": state.get("director_service_tier", "default"),
                    "usage": state.get("director_usage"),
                }
            else:
                response = {
                    "id": response.get("id"),
                    "model": response.get("model") or state.get("director_model"),
                    "service_tier": response.get("service_tier") or "default",
                    "usage": response.get("usage"),
                }
            current = next(
                (
                    index for index, item in enumerate(self._records)
                    if item.get("director_state_file") == source
                ),
                None,
            )
            if current is not None:
                self._records.pop(current)
            self.record("Fish director", state.get("block_id"), response)
            self._records[-1]["director_state_file"] = source

    def restore_requests(self, requests: list[object]) -> None:
        """Carry priced request history forward when continuing a prior run."""
        for request in requests:
            if not isinstance(request, Mapping) or request.get("stage") not in _STAGES:
                raise ValueError("Cannot restore malformed API request history")
            self._records.append(dict(request))

    def sync_image_usage(self, output: Path) -> None:
        """Rebuild image charges from durable POST states, including replaced images.

        Rebuilding rather than appending makes resumes and repeat reconciliations
        idempotent. An unknown POST outcome is never interpreted as a free call.
        """
        self._records = [
            item for item in self._records if item.get("stage") != _IMAGE_STAGE
        ]
        self._expected_image_calls = 0
        root = output / "images"
        manifest_path = root / "manifest.json"
        archive = root / "replaced_states"
        if any(path.is_symlink() for path in (root, manifest_path, archive, root / "grids")):
            raise ValueError("Refusing linked image accounting state")

        planned: int | None = None
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("Invalid image accounting manifest")
            planned = _tokens(manifest.get("total_generation_jobs"))
            if planned is None and "total_generation_jobs" in manifest:
                raise ValueError("Invalid planned image request count")
            additional = _tokens(manifest.get("native_grid_video_calls", 0))
            if additional is None:
                raise ValueError("Invalid native professional grid-video request count")
            if planned is not None:
                planned += additional

        finished_archive = output / "images_replaced_professional_finishing"
        if finished_archive.is_symlink():
            raise ValueError("Refusing linked professional finishing billing archive")
        active = sorted(root.glob("block_*/*.json")) + sorted(
            (root / "grids").glob("*.json")
        ) + sorted(
            (root / "finishing_state").glob("block_*/*-native_grid_video.json")
        ) + sorted(
            (root / "professional_finished").glob("block_*/*-native_grid_video.json")
        )
        replaced = sorted(archive.glob("block_*/*.json")) + sorted(
            finished_archive.glob("run-*/block_*/*-native_grid_video.json")
        )
        active_count = 0
        replaced_count = 0
        for path in (*active, *replaced):
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError(f"Refusing linked image accounting state: {path}")
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("schema_version") not in {
                "openai-image-v1", "professional-image-finishing-v1",
            }:
                raise ValueError(f"Invalid image accounting state: {path}")
            status = state.get("status")
            if status not in ("completed", "direct_submitting_unknown"):
                continue  # direct_pending has not been submitted to the paid API.
            if path.is_relative_to(archive) or path.is_relative_to(finished_archive):
                replaced_count += 1
            else:
                active_count += 1

            artifact = state.get("artifact")
            if not isinstance(artifact, dict):
                artifact = {}
            usage = artifact.get("openai_usage")
            details = _field(usage, "input_tokens_details")
            input_tokens = _tokens(_field(usage, "input_tokens"))
            text_tokens = _tokens(_field(details, "text_tokens"))
            image_tokens = _tokens(_field(details, "image_tokens"))
            output_tokens = _tokens(_field(usage, "output_tokens"))
            model = artifact.get("model_id") or state.get("model_id")
            record: dict[str, object] = {
                "stage": _IMAGE_STAGE,
                "block_id": state.get("block_id"),
                "beat_id": state.get("beat_id"),
                "image_state_file": path.relative_to(output).as_posix(),
                "image_request_status": status,
                "model": model,
                "service_tier": "standard",
                "input_tokens": input_tokens,
                "text_input_tokens": text_tokens,
                "image_input_tokens": image_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": None,
                "pricing_note": None,
            }
            if status != "completed":
                record["pricing_note"] = "Image POST outcome is unknown; charge may apply"
            elif model not in _IMAGE_MODELS:
                record["pricing_note"] = "No verified price for returned image model"
            elif (
                input_tokens is None
                or text_tokens is None
                or image_tokens is None
                or output_tokens is None
                or text_tokens + image_tokens != input_tokens
            ):
                record["pricing_note"] = "Missing or inconsistent image API token usage"
            else:
                amount = (
                    Decimal(text_tokens) * _IMAGE_PRICING["text_input"]
                    + Decimal(image_tokens) * _IMAGE_PRICING["image_input"]
                    + Decimal(output_tokens) * _IMAGE_PRICING["image_output"]
                ) / _PER_MILLION
                record["estimated_cost_usd"] = _usd(amount)
            self._records.append(record)
        self._expected_image_calls = replaced_count + max(
            planned if planned is not None else active_count, active_count
        )

    def record_cta_image(self, usage: dict[str, object] | None, model: str) -> None:
        """Account only for the first actually generated CTA image."""
        if any(entry["stage"] == "CTA image" for entry in self._records):
            return
        details = _field(usage, "input_tokens_details")
        text_tokens = _tokens(_field(details, "text_tokens"))
        image_tokens = _tokens(_field(details, "image_tokens"))
        input_tokens = _tokens(_field(usage, "input_tokens"))
        output_tokens = _tokens(_field(usage, "output_tokens"))
        known = (
            model in _IMAGE_MODELS and input_tokens is not None
            and text_tokens is not None and image_tokens is not None
            and output_tokens is not None and input_tokens == text_tokens + image_tokens
        )
        amount = (
            (Decimal(text_tokens) * _IMAGE_PRICING["text_input"]
             + Decimal(image_tokens) * _IMAGE_PRICING["image_input"]
             + Decimal(output_tokens) * _IMAGE_PRICING["image_output"]) / _PER_MILLION
            if known else None
        )
        self._records.append({
            "stage": "CTA image", "block_id": None, "model": model,
            "service_tier": "standard", "input_tokens": input_tokens,
            "text_input_tokens": text_tokens, "image_input_tokens": image_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": _usd(amount) if amount is not None else None,
            "pricing_note": None if known else "Unavailable CTA image token usage",
        })

    def report(
        self,
        *,
        blocks: int | None,
        run_status: Literal["running", "completed", "failed"],
        expected_b11_calls: int = 1,
        expected_calls: Mapping[str, int | None] | None = None,
    ) -> dict[str, object]:
        ordered = sorted(
            self._records,
            key=lambda item: (_STAGES.index(str(item["stage"])), item["block_id"] or 0),
        )
        expected = (
            {"B1.1": max(1, expected_b11_calls), "CTA": 0, "CTA Ebook": 0,
             "CTA Motion": 0,
             "B1.2": blocks, "B2": blocks, "CTA image": 0}
            if expected_calls is None
            else {
                stage: expected_calls.get(
                    stage, 0 if stage in {"CTA", "CTA Ebook", "CTA Motion", "CTA image"} else None
                )
                for stage in _STAGES
            }
        )
        previous_image_count = expected.get(_IMAGE_STAGE)
        expected[_IMAGE_STAGE] = max(
            self._expected_image_calls,
            previous_image_count
            if type(previous_image_count) is int and previous_image_count >= 0
            else 0,
        )
        expected["CTA image"] = sum(item["stage"] == "CTA image" for item in ordered)
        fish_count = sum(item["stage"] == "Fish director" for item in ordered)
        for v_stage in ("V1", "V2", "V3"):
            actual = sum(item["stage"] == v_stage for item in ordered)
            reported = expected_calls.get(v_stage) if expected_calls is not None else None
            expected[v_stage] = max(
                actual, reported if type(reported) is int and reported >= 0 else 0,
            )
        expected["Fish director"] = (
            fish_count if expected_calls is None
            else expected_calls.get("Fish director", fish_count)
        )
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
            "included": "GPT-6 Luna B/V planning, Fish director and GPT Image 2.5 calls",
            "excluded": "OpenAI STT and all Gemini/AI33 TTS provider charges",
            "run_status": run_status,
            "pricing_status": "complete" if complete else "partial_or_unavailable",
            "pricing_model": "gpt-6-luna",
            "pricing_tier": "Standard",
            "pricing_as_of": PRICING_AS_OF,
            "pricing_source": PRICING_URL,
            "pricing_per_million_usd": {
                key: str(value) for key, value in _STANDARD_LUNA.items()
            },
            "image_pricing_models": sorted(_IMAGE_MODELS),
            "image_pricing_per_million_usd": {
                key: str(value) for key, value in _IMAGE_PRICING.items()
            },
            "estimated_total_usd": _usd(subtotal) if complete else None,
            "priced_subtotal_usd": _usd(subtotal),
            "stages": stages,
            "requests": ordered,
        }
