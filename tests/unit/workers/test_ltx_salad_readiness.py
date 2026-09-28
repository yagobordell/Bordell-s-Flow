from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest

from ai_video_factory.inference.salad import SaladRequestError
from scripts.smoke.wait_salad_ltx25_ready import _readiness_observation, wait_for_ready

GROUP_NAME = "ai-video-factory-ltx25-worker-v3"
IMAGE_REPO = "docker.io/yagobordell/ai-video-factory"
PINNED_IMAGE = f"{IMAGE_REPO}@sha256:{'a' * 64}"


def _group() -> dict[str, Any]:
    return {
        "name": GROUP_NAME,
        "version": 9,
        "container": {"image": PINNED_IMAGE},
        "readiness_probe": {"http": {"path": "/ready", "port": 8080}},
        "current_state": {"status": "running"},
        "pending_change": False,
        "replicas": 1,
    }


def _instance(*, instance_id: str = "gpu-one", ready: bool = False) -> dict[str, object]:
    return {
        "id": instance_id,
        "version": 9,
        "state": "running",
        "started": True,
        "ready": ready,
    }


class _Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def now(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


class _Salad:
    def __init__(
        self,
        observations: list[tuple[dict[str, Any], list[dict[str, object]]]],
        *,
        error: Exception | None = None,
    ) -> None:
        self.observations = observations
        self.index = 0
        self.error = error

    def describe_container_group(self) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return self.observations[min(self.index, len(self.observations) - 1)][0]

    def list_container_group_instances(self) -> list[dict[str, object]]:
        result = self.observations[min(self.index, len(self.observations) - 1)][1]
        self.index += 1
        return result


def _wait(
    client: _Salad,
    *,
    timeout: float = 10,
    clock: _Clock | None = None,
    report: Callable[[str], None] | None = None,
) -> str:
    timer = clock or _Clock()
    return wait_for_ready(
        client,  # type: ignore[arg-type]
        group_name=GROUP_NAME,
        image_repository=IMAGE_REPO,
        expected_image=PINNED_IMAGE,
        timeout_seconds=timeout,
        poll_seconds=1,
        clock=timer.now,
        sleep=timer.sleep,
        report=report or (lambda _: None),
    )


def test_ready_requires_two_polls_after_bootstrap_and_never_submits_a_job() -> None:
    observations = [
        (_group(), [_instance()]),
        (_group(), [_instance(ready=True)]),
        (_group(), [_instance(ready=True)]),
    ]
    timer = _Clock()
    reports: list[str] = []
    client = _Salad(observations)
    assert _wait(client, clock=timer, report=reports.append) == "gpu-one"
    assert timer.elapsed == 2
    assert client.index == 3
    assert any("bootstrap_wait_seconds=2.0" in line for line in reports)


def test_readiness_flap_or_instance_replacement_resets_stable_count() -> None:
    client = _Salad(
        [
            (_group(), [_instance(instance_id="old", ready=True)]),
            (_group(), [_instance(instance_id="replacement", ready=True)]),
            (_group(), [_instance(instance_id="replacement", ready=False)]),
            (_group(), [_instance(instance_id="replacement", ready=True)]),
            (_group(), [_instance(instance_id="replacement", ready=True)]),
        ]
    )
    timer = _Clock()
    assert _wait(client, clock=timer) == "replacement"
    assert timer.elapsed == 4


def test_ready_instance_can_be_accepted_during_group_deployment() -> None:
    group = _group()
    group["current_state"]["status"] = "deploying"
    client = _Salad([(group, [_instance(ready=True)])])
    assert _wait(client) == "gpu-one"


def test_readiness_rejects_undocumented_instance_id_field() -> None:
    instance = _instance(ready=True)
    instance["instance_id"] = instance.pop("id")
    with pytest.raises(RuntimeError, match="without an instance ID"):
        _readiness_observation(
            _group(), [instance], group_name=GROUP_NAME, image_repository=IMAGE_REPO
        )


def test_readiness_timeout_does_not_silently_accept_unready_instance() -> None:
    timer = _Clock()
    with pytest.raises(TimeoutError, match="no inference job was submitted"):
        _wait(_Salad([(_group(), [_instance()])]), timeout=3, clock=timer)
    assert timer.elapsed == 3


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda group: group.update({"readiness_probe": {}}), "/ready"),
        (lambda group: group["container"].update({"image": "docker.io/tag:latest"}), "SHA256"),
        (lambda group: group.update({"name": "other-group"}), "unexpected"),
        (lambda group: group.update({"version": None}), "version"),
        (lambda group: group.update({"replicas": 2}), "exactly one"),
        (
            lambda group: group.update({"current_state": {"status": "stopped"}}),
            "stopped",
        ),
    ],
)
def test_unsafe_group_configuration_fails_closed(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    group = _group()
    mutate(group)
    with pytest.raises(RuntimeError, match=message):
        _readiness_observation(
            group,
            [_instance(ready=True)],
            group_name=GROUP_NAME,
            image_repository=IMAGE_REPO,
            expected_image=PINNED_IMAGE,
        )


def test_pinned_image_and_current_instance_version_must_match() -> None:
    group = _group()
    with pytest.raises(RuntimeError, match="does not match"):
        _readiness_observation(
            group,
            [_instance(ready=True)],
            group_name=GROUP_NAME,
            image_repository=IMAGE_REPO,
            expected_image=f"{IMAGE_REPO}@sha256:{'b' * 64}",
        )
    stale = deepcopy(_instance(ready=True))
    stale["version"] = 8
    ready_id, description = _readiness_observation(
        group,
        [stale],
        group_name=GROUP_NAME,
        image_repository=IMAGE_REPO,
    )
    assert ready_id is None
    assert "running_current_instances=0" in description


def test_fail_closed_after_bounded_transient_salad_errors() -> None:
    error = SaladRequestError(
        message="transient", status_code=429, url="https://api.salad.com", detail=""
    )
    timer = _Clock()
    with pytest.raises(RuntimeError, match="five consecutive polls"):
        _wait(_Salad([], error=error), clock=timer)
    assert timer.elapsed == 4


def test_nontransient_salad_error_is_not_masked() -> None:
    error = SaladRequestError(
        message="unauthorized", status_code=401, url="https://api.salad.com", detail=""
    )
    timer = _Clock()
    with pytest.raises(SaladRequestError, match="unauthorized"):
        _wait(_Salad([], error=error), clock=timer)
    assert timer.elapsed == 0
