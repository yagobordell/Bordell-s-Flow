"""B2.2 observations never alter motion outputs or double-charge cache hits."""

from __future__ import annotations

import json
from types import SimpleNamespace

from ai_video_factory.bots.billing import ApiCostLedger
from ai_video_factory.workflows.b22_motion import adapt_b22_motion


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured_with_response(self, **kwargs):
        from ai_video_factory.workflows.b22_motion import B22Beat, B22Result

        self.calls += 1
        manifest = json.loads(kwargs["input_text"].split("\n", 1)[1])
        return B22Result(beats=[
            B22Beat(beat_id=key, video_prompt="Carefully tilt the device.")
            for key in manifest["expected_output_beat_ids"]
        ]), SimpleNamespace(
            id="response-1", model="gpt-6-luna", service_tier="default",
            usage=SimpleNamespace(
                input_tokens=400, output_tokens=160,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=0, cache_write_tokens=0,
                ),
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )


def test_b22_progress_and_cache_usage_are_durable_and_idempotent(tmp_path):
    image = tmp_path / "frame.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake image")
    beat = {
        "beat_id": "1A", "block_id": 1, "beat_type": "action",
        "text": "The device buzzes unexpectedly.", "visual_type": "media_video",
        "audio": False, "video_prompt": "Pick up the phone.",
        "start_seconds": 0.0, "end_seconds": 3.0,
    }
    shot = SimpleNamespace(
        beats=("1A",), kind="i2v", duration_seconds=3.0, image=image,
        request=SimpleNamespace(parameters={"prompt": "Pick up the phone."}),
    )
    provider = FakeProvider()
    for _ in range(2):
        result = adapt_b22_motion(
            tmp_path, {"beats": [beat]}, [shot], {},
            provider=provider, model="gpt-6-luna",
        )
        assert result == {"1A": "Carefully tilt the device."}
    assert provider.calls == 1
    status = json.loads(
        (tmp_path / "video" / "b22" / "state" / "progress.json").read_text()
    )
    assert status["status"] == "completed"
    assert status["groups_completed"] == status["groups_total"] == 1
    assert status["groups"][0]["cache_hit"] is True

    ledger = ApiCostLedger()
    ledger.sync_b22_usage(tmp_path)
    assert len([r for r in ledger.records if r["stage"] == "B2.2"]) == 1
    assert next(r for r in ledger.records if r["stage"] == "B2.2")[
        "estimated_cost_usd"
    ] is not None
    snapshot = ledger.report(blocks=1, run_status="completed")
    assert snapshot["stages"]["B2.2"]["recorded_calls"] == 1
    ledger.restore_requests(list(snapshot["requests"]))
    ledger.sync_b22_usage(tmp_path)
    assert len([r for r in ledger.records if r["stage"] == "B2.2"]) == 1

    files = list((tmp_path / "video" / "b22" / "usage").glob("*.json"))
    assert len(files) == 1
    assert "Carefully tilt the device." not in files[0].read_text()


def test_missing_b22_usage_does_not_add_charges(tmp_path):
    ledger = ApiCostLedger()
    ledger.sync_b22_usage(tmp_path)
    assert not [r for r in ledger.records if r["stage"] == "B2.2"]
