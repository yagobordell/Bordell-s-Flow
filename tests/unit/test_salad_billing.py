"""Salad GPU accounting: running-state time, replica count and incomplete samples."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_video_factory.inference.salad_billing import (
    SaladCostMeter,
    _rate,
    read_salad_costs,
)


class EmptyClient:
    def list_container_group_instances(self):
        return []


@pytest.fixture
def services_path(tmp_path):
    path = tmp_path / "services.json"
    path.write_text(
        json.dumps(
            {
                "services": {
                    service: {
                        "group_name": f"group-{service}",
                        "priority": "high",
                        "resources": {"gpu_class_names": ["RTX 5090 (32 GB)"]},
                    }
                    for service in ("ltx25_i2v", "ltx25")
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _meter(tmp_path, services_path):
    return SaladCostMeter(
        tmp_path,
        services_path=services_path,
        api_key="test-key",
        organization="test-org",
        project="test-project",
        clients={"ltx25_i2v": EmptyClient(), "ltx25": EmptyClient()},
    )


def _instance(name, state, transition):
    return {"id": name, "state": state, "update_time": transition.isoformat()}


def test_running_seconds_count_each_replica_not_downloading(tmp_path, services_path):
    t = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    with _meter(tmp_path, services_path) as meter:
        meter._session["started_at"] = t.isoformat()
        meter._observe(
            "ltx25",
            [
                _instance("a", "running", t + timedelta(seconds=2)),
                _instance("b", "running", t + timedelta(seconds=5)),
                _instance("c", "downloading", t),
            ],
            t + timedelta(seconds=12),
        )
        meter._observe(
            "ltx25",
            [
                _instance("a", "running", t + timedelta(seconds=2)),
                _instance("b", "stopping", t + timedelta(seconds=15)),
                _instance("c", "running", t + timedelta(seconds=13)),
            ],
            t + timedelta(seconds=20),
        )
        # a=18s, b=10s, c=7s; the first 13s of downloading is free.
        assert meter._session["services"]["ltx25"]["gpu_running_seconds"] == 35
        assert meter._session["services"]["ltx25_i2v"]["gpu_running_seconds"] == 0
    summary = read_salad_costs(meter.path)
    assert summary is not None
    assert summary["services"]["ltx25"]["gpu_running_seconds"] == 35
    assert Decimal(summary["priced_subtotal_usd"]) == Decimal("0.00486111")
    assert summary["pricing_status"] == "partial"


def test_unknown_price_is_not_guessed(monkeypatch):
    monkeypatch.delenv("SALAD_LTX25_USD_PER_GPU_HOUR", raising=False)
    definition = {
        "priority": "low",
        "resources": {"gpu_class_names": ["RTX 4090 (24 GB)"]},
    }
    assert _rate("ltx25", definition) is None
    monkeypatch.setenv("SALAD_LTX25_USD_PER_GPU_HOUR", "0.75")
    assert _rate("ltx25", definition) == "0.75"
    monkeypatch.setenv("SALAD_LTX25_USD_PER_GPU_HOUR", "-2")
    assert _rate("ltx25", definition) is None


def test_missing_credentials_do_not_produce_fake_zero_cost(tmp_path, services_path):
    with SaladCostMeter(
        tmp_path,
        services_path=services_path,
        api_key=None,
        organization=None,
        project=None,
    ) as meter:
        pass
    summary = read_salad_costs(meter.path)
    assert summary is not None
    assert summary["pricing_status"] == "unavailable"
    assert summary["priced_subtotal_usd"] is None
    assert summary["estimated_observed_cost_usd"] is None


def test_existing_running_session_is_marked_interrupted_on_resume(
    tmp_path, services_path
):
    meter = _meter(tmp_path, services_path)
    with meter:
        pass
    ledger = json.loads(meter.path.read_text(encoding="utf-8"))
    ledger["sessions"][0]["status"] = "running"
    meter.path.write_text(json.dumps(ledger), encoding="utf-8")
    with _meter(tmp_path, services_path):
        pass
    saved = json.loads(meter.path.read_text(encoding="utf-8"))
    assert [session["status"] for session in saved["sessions"]] == [
        "interrupted", "completed"
    ]
    assert read_salad_costs(meter.path)["pricing_status"] == "partial"
