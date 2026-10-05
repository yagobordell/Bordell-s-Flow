"""Offline tests for demand-driven startup of the independent Salad image upscaler."""

from __future__ import annotations

import json

import pytest

from ai_video_factory.inference import salad_image_upscaler_start as startup


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "upscaler.json"
    path.write_text(
        json.dumps({"schema_version": 1, "service": {"group_name": "bordell-image-upscaler-v2"}}),
        encoding="utf-8",
    )
    return path


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSalad:
    def __init__(self, *, states, instances=(), start_result=True, dns="gateway.salad.cloud"):
        self.states = list(states)
        self.instances = list(instances)
        self.start_result = start_result
        self.dns = dns
        self.start_calls = 0
        self.describe_calls = 0
        self.instance_calls = 0

    def describe_container_group(self):
        index = min(self.describe_calls, len(self.states) - 1)
        self.describe_calls += 1
        state, pending = self.states[index]
        return {
            "current_state": {"status": state},
            "pending_change": pending,
            "networking": {"dns": self.dns},
        }

    def list_container_group_instances(self):
        self.instance_calls += 1
        index = min(self.instance_calls - 1, len(self.instances) - 1)
        return self.instances[index] if self.instances else []

    def start_container_group_if_needed(self, *, warning_logger):
        self.start_calls += 1
        return self.start_result


READY = [{"ready": True, "state": "running"}]
NOT_READY = [{"ready": False, "state": "running"}]


def check(client, manifest, *, auto_start=True, clock=None, timeout=120):
    clock = clock or Clock()
    messages = []
    result = startup.ensure_image_upscaler_ready(
        api_key="fake-secret",
        gateway_url="https://gateway.salad.cloud",
        auto_start=auto_start,
        timeout_seconds=timeout,
        poll_seconds=5,
        manifest_path=manifest,
        client=client,
        clock=clock,
        sleep=clock.sleep,
        notify=messages.append,
    )
    return result, messages


def test_stopped_group_starts_once_and_waits_until_ready(manifest):
    salad = FakeSalad(
        states=[("stopped", False), ("deploying", False), ("running", False),
                ("running", False)],
        instances=[NOT_READY, READY],
    )
    started, messages = check(salad, manifest)
    assert started is True
    assert salad.start_calls == 1
    assert salad.instance_calls == 2
    assert any("instance ready" in message for message in messages)


def test_already_running_group_reuses_ready_instance(manifest):
    salad = FakeSalad(states=[("running", False)], instances=[READY])
    started, _ = check(salad, manifest)
    assert started is False
    assert salad.start_calls == 0


def test_deploying_group_waits_without_sending_another_start(manifest):
    salad = FakeSalad(
        states=[("deploying", True), ("deploying", False), ("running", False)],
        instances=[READY],
    )
    started, _ = check(salad, manifest)
    assert started is False
    assert salad.start_calls == 0


def test_pending_stopped_group_waits_for_settlement(manifest):
    salad = FakeSalad(
        states=[("stopped", True), ("stopped", False), ("running", False)],
        instances=[READY],
    )
    started, _ = check(salad, manifest)
    assert started is True
    assert salad.start_calls == 1


def test_manual_opt_out_only_fails_if_stopped(manifest):
    stopped = FakeSalad(states=[("stopped", False)])
    with pytest.raises(startup.ImageUpscalerStartupError, match="AUTO_START=false"):
        check(stopped, manifest, auto_start=False)
    assert stopped.start_calls == 0
    running = FakeSalad(states=[("running", False)], instances=[READY])
    started, _ = check(running, manifest, auto_start=False)
    assert started is False


def test_stale_gateway_rejected_before_gpu_start(manifest):
    salad = FakeSalad(states=[("stopped", False)], dns="different.salad.cloud")
    with pytest.raises(startup.ImageUpscalerStartupError, match="does not match"):
        check(salad, manifest)
    assert salad.start_calls == 0


def test_readiness_timeout_has_actionable_error(manifest):
    salad = FakeSalad(states=[("deploying", False)])
    with pytest.raises(startup.ImageUpscalerStartupError, match="did not become ready"):
        check(salad, manifest, timeout=10)
    assert salad.start_calls == 0


def test_failed_group_fails_closed(manifest):
    salad = FakeSalad(states=[("failed", False)])
    with pytest.raises(startup.ImageUpscalerStartupError, match="instance logs"):
        check(salad, manifest)


def test_racing_start_tolerated_without_duplicate_request(manifest):
    salad = FakeSalad(
        states=[("stopped", False), ("deploying", False), ("running", False)],
        instances=[READY],
        start_result=False,
    )
    started, _ = check(salad, manifest)
    assert started is False
    assert salad.start_calls == 1


def test_racing_start_does_not_repeat_post_if_get_is_eventually_consistent(manifest):
    salad = FakeSalad(
        states=[
            ("stopped", False), ("stopped", False), ("stopped", False),
            ("deploying", False), ("running", False),
        ],
        instances=[READY],
        start_result=False,
    )
    started, _ = check(salad, manifest)
    assert started is False
    assert salad.start_calls == 1


def test_missing_salad_account_fails_before_network(monkeypatch, manifest):
    monkeypatch.setattr(startup, "_account_settings", lambda: (None, "project"))
    with pytest.raises(startup.ImageUpscalerStartupError, match="SALAD_ORGANIZATION"):
        startup.ensure_image_upscaler_ready(
            api_key="fake-secret",
            gateway_url="https://gateway.salad.cloud",
            manifest_path=manifest,
        )


def test_pipeline_generates_paid_originals_before_waiting_for_salad():
    from pathlib import Path

    runner = Path("scripts/pipeline/run_b_pipeline.py").read_text(encoding="utf-8")
    function = runner.split("async def _generate_images(", 1)[1].split(
        "\nasync def ", 1
    )[0]
    assert "needs_x2 = professional and any(" in function
    assert "if needs_x2:" in function
    assert function.index("generate_b2_images,") < function.index(
        "crop_grid_assets,"
    ) < function.index("ensure_image_upscaler_ready,")
    assert function.index('manifest["finishing_status"] = "pending_upscaler"') < (
        function.index("ensure_image_upscaler_ready,")
    )
    assert function.index("ensure_image_upscaler_ready,") < function.index(
        "finish_professional_images,"
    )
