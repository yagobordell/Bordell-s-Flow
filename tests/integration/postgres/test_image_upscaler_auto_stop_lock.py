from __future__ import annotations

import os

import pytest

from ai_video_factory.inference.salad_image_upscaler_start import image_upscaler_usage


def _postgres_dsn() -> str:
    dsn = os.getenv("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    return dsn


class FakeSalad:
    def __init__(self) -> None:
        self.stop_calls = 0

    def describe_container_group(self):
        return {
            "current_state": {"status": "running"},
            "pending_change": False,
            "networking": {"dns": "gateway.salad.cloud"},
        }

    def list_container_group_instances(self):
        return [{"ready": True, "state": "running"}]

    def start_container_group_if_needed(self, *, warning_logger):
        del warning_logger
        raise AssertionError("already-running fake must not be started")

    def stop_container_group(self):
        self.stop_calls += 1


def test_last_cross_connection_upscaler_user_is_the_only_one_that_stops() -> None:
    dsn = _postgres_dsn()
    outer = FakeSalad()
    inner = FakeSalad()

    with image_upscaler_usage(
        api_key="fake-secret",
        gateway_url="https://gateway.salad.cloud",
        postgres_dsn=dsn,
        client=outer,
        notify=lambda _message: None,
    ):
        with image_upscaler_usage(
            api_key="fake-secret",
            gateway_url="https://gateway.salad.cloud",
            postgres_dsn=dsn,
            client=inner,
            notify=lambda _message: None,
        ):
            pass

        # The inner run finished, but the outer shared advisory lock proves
        # another cooperating run is still active, so it must not stop Salad.
        assert inner.stop_calls == 0
        assert outer.stop_calls == 0

    # Once the final shared holder exits, it can take the exclusive advisory
    # lock and safely request Stop without racing another cooperating run.
    assert outer.stop_calls == 1
    assert inner.stop_calls == 0
