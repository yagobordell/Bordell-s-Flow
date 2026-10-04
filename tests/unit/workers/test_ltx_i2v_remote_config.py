"""Free regression gates for the exact Salad I2V deployment digest and resources."""

from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.smoke.verify_ltx25_i2v_remote_config import (
    inspect_remote,
    verify_remote_group,
)

IMAGE = "docker.io/yagobordell/ai-video-factory@sha256:" + "a" * 64
GROUP = "ai-video-factory-ltx25-i2v-worker-v1"


def group() -> dict:
    return {
        "name": GROUP,
        "current_state": {"status": "stopped"},
        "pending_change": False,
        "container": {
            "image": IMAGE,
            "environment_variables": {
                "LTX_WORKER_TASK_MODE": "i2v",
                "LTX_EXPERIMENTAL_I2V_BATCH2_ENABLED": "true",
            },
            "resources": {"memory": 61440},
        },
    }


def verify(state: dict) -> dict:
    return verify_remote_group(
        state, expected_group=GROUP, expected_image=IMAGE, minimum_memory_mb=61440
    )


def test_remote_immutable_digest_and_memory_are_verified_read_only() -> None:
    class ReadOnlyClient:
        def __init__(self) -> None:
            self.calls = 0

        def describe_container_group(self) -> dict:
            self.calls += 1
            return group()

    client = ReadOnlyClient()
    report = inspect_remote(
        client, expected_group=GROUP, expected_image=IMAGE, minimum_memory_mb=61440
    )
    assert client.calls == 1
    assert report["memory_mb"] == 61440
    assert report["image"] == IMAGE
    assert report["status"] == "stopped"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("name", "other-group", "Wrong Salad"),
        ("pending_change", True, "stably STOPPED"),
        ("current_state", {"status": "running"}, "stably STOPPED"),
        ("container.image", "docker.io/other@sha256:" + "b" * 64, "digest differs"),
        ("container.environment_variables.LTX_WORKER_TASK_MODE", "a2v", "isolated I2V"),
        (
            "container.environment_variables.LTX_EXPERIMENTAL_I2V_BATCH2_ENABLED",
            "false",
            "isolated I2V",
        ),
        ("container.resources.memory", 40960, "below"),
        ("container.resources.memory", None, "absent"),
    ],
)
def test_remote_mismatch_aborts_before_any_mutation(
    field: str, value: object, error: str
) -> None:
    state = deepcopy(group())
    target = state
    path = field.split(".")
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    with pytest.raises(RuntimeError, match=error):
        verify(state)


def test_unknown_memory_and_unpinned_expected_image_fail_closed() -> None:
    state = group()
    state["container"]["resources"] = None
    with pytest.raises(RuntimeError, match="absent"):
        verify(state)
    with pytest.raises(ValueError, match="immutable"):
        verify_remote_group(group(), expected_group=GROUP, expected_image="latest")
