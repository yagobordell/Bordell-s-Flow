from __future__ import annotations

import json
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_video_factory.inference.salad import SaladRequestError
from scripts.smoke import wait_salad_ltx25_ready as readiness
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
    expected_task_mode: str = "",
) -> str:
    timer = clock or _Clock()
    return wait_for_ready(
        client,  # type: ignore[arg-type]
        group_name=GROUP_NAME,
        image_repository=IMAGE_REPO,
        expected_image=PINNED_IMAGE,
        expected_task_mode=expected_task_mode,
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


def test_i2v_recovers_from_one_522_during_image_download() -> None:
    """An edge timeout must not abort an otherwise healthy image pull."""
    error = SaladRequestError(
        message="Cloudflare 522", status_code=522, url="https://api.salad.com", detail=""
    )

    class _FlakyI2V(_Salad):
        failed = False

        def describe_container_group(self) -> dict[str, Any]:
            if not self.failed:
                self.failed = True
                raise error
            return super().describe_container_group()

    client = _FlakyI2V([(_group(), [_instance(ready=True)])])
    clock = _Clock()
    reports: list[str] = []
    assert _wait(
        client, clock=clock, report=reports.append, expected_task_mode="i2v"
    ) == "gpu-one"
    assert clock.elapsed == 2
    assert any("transient status=522" in report for report in reports)


def test_i2v_522_still_fails_after_bounded_retries() -> None:
    error = SaladRequestError(
        message="Cloudflare 522", status_code=522, url="https://api.salad.com", detail=""
    )
    clock = _Clock()
    with pytest.raises(RuntimeError, match="five consecutive polls"):
        _wait(_Salad([], error=error), clock=clock, expected_task_mode="i2v")
    assert clock.elapsed == 4


def test_a2v_does_not_inherit_i2v_only_522_retry_rule() -> None:
    error = SaladRequestError(
        message="Cloudflare 522", status_code=522, url="https://api.salad.com", detail=""
    )
    clock = _Clock()
    with pytest.raises(SaladRequestError, match="Cloudflare 522"):
        _wait(_Salad([], error=error), clock=clock, expected_task_mode="a2v")
    assert clock.elapsed == 0


def test_nontransient_salad_error_is_not_masked() -> None:
    error = SaladRequestError(
        message="unauthorized", status_code=401, url="https://api.salad.com", detail=""
    )
    timer = _Clock()
    with pytest.raises(SaladRequestError, match="unauthorized"):
        _wait(_Salad([], error=error), clock=timer)
    assert timer.elapsed == 0


def _local_ready_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_legacy_manifest_account: bool = False,
) -> None:
    """A standalone read-only CLI fixture, independent of any real Salad connection."""
    deploy = tmp_path / "deploy" / "salad"
    deploy.mkdir(parents=True)
    stack = (
        {"organization": "wrong-organization", "project": "wrong-project"}
        if include_legacy_manifest_account
        else {}
    )
    (deploy / "services.json").write_text(
        json.dumps(
            {
                "stack": stack,
                "services": {
                    "ltx25_i2v": {
                        "group_name": "i2v-test-group",
                        "image": "docker.io/yagobordell/ai-video-factory:historic-tag",
                        "environment": {"LTX_WORKER_TASK_MODE": "i2v"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "SALAD_ORGANIZATION=correct-organization\n"
        "SALAD_PROJECT=correct-project\n"
        "SALAD_API_KEY=dummy-test-only\n",
        encoding="utf-8",
    )
    for name in ("SALAD_ORGANIZATION", "SALAD_PROJECT", "SALAD_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(readiness, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wait_salad_ltx25_ready.py",
            "--service",
            "ltx25_i2v",
            "--expected-image",
            PINNED_IMAGE,
        ],
    )


@pytest.mark.parametrize("legacy_manifest_account", [False, True])
def test_readiness_cli_selects_explicit_account_from_env_not_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_manifest_account: bool,
) -> None:
    _local_ready_configuration(
        tmp_path,
        monkeypatch,
        include_legacy_manifest_account=legacy_manifest_account,
    )
    captured: list[object] = []

    def make_client(config: object) -> SimpleNamespace:
        captured.append(config)
        return SimpleNamespace(config=config)

    def accept_ready(client: object, **kwargs: object) -> str:
        captured.append((client, kwargs))
        return "fake-instance"

    monkeypatch.setattr(readiness, "SaladClient", make_client)
    monkeypatch.setattr(readiness, "wait_for_ready", accept_ready)
    assert readiness.main() == 0
    selected = captured[0]
    assert selected.organization_name == "correct-organization"
    assert selected.project_name == "correct-project"
    assert selected.container_group_name == "i2v-test-group"
    assert len(captured) == 2


def test_readiness_local_configuration_preflight_never_contacts_salad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _local_ready_configuration(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--check-config-only"])
    monkeypatch.setattr(
        readiness,
        "SaladClient",
        lambda _: pytest.fail("Config preflight must not contact Salad"),
    )
    assert readiness.main() == 0


@pytest.mark.parametrize(
    ("env_name", "replacement"),
    [
        ("SALAD_ORGANIZATION", ""),
        ("SALAD_PROJECT", ""),
        ("SALAD_ORGANIZATION", "invalid/name"),
        ("SALAD_PROJECT", "invalid name"),
    ],
)
def test_readiness_fails_closed_for_missing_or_unsafe_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    replacement: str,
) -> None:
    _local_ready_configuration(tmp_path, monkeypatch)
    # Non-empty process environment wins over .env, as in the PowerShell manager.
    if not replacement:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                line
                for line in env_file.read_text(encoding="utf-8").splitlines()
                if not line.startswith(env_name + "=")
            ) + "\n",
            encoding="utf-8",
        )
    else:
        monkeypatch.setenv(env_name, replacement)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--check-config-only"])
    monkeypatch.setattr(
        readiness,
        "SaladClient",
        lambda _: pytest.fail("Invalid account must fail before a Salad client exists"),
    )
    with pytest.raises(SystemExit, match=env_name):
        readiness.main()
