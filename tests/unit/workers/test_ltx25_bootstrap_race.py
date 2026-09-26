from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path("scripts/smoke/start_ltx25_race_select.py")
IMAGE = "docker.io/example/worker@sha256:" + "a" * 64
GROUP = "ai-video-factory-ltx25-worker-v3"
WINNER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
LOSER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _module():
    spec = importlib.util.spec_from_file_location("ltx_race_selector", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    seconds = 0.0

    def clock(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


class FakeRaceClient:
    def __init__(
        self, *, ready: bool = True, image: str = IMAGE, wrong_winner: bool = False
    ) -> None:
        self.replicas = 1
        self.status = "stopped"
        self.image = image
        self.ready = ready
        self.wrong_winner = wrong_winner
        self.deletion_costs: dict[str, int] = {}
        self.events: list[str] = []
        self.version = 13

    def describe_container_group(self) -> dict[str, Any]:
        return {
            "name": GROUP,
            "version": self.version,
            "replicas": self.replicas,
            "pending_change": False,
            "current_state": {"status": self.status},
            "container": {"image": self.image},
            "readiness_probe": {"http": {"path": "/ready"}},
        }

    def list_container_group_instances(self) -> list[dict[str, Any]]:
        if self.status == "stopped":
            return []
        if self.replicas == 1:
            instance_id = LOSER if self.wrong_winner else WINNER
            return [self._instance(instance_id, ready=True)]
        return [
            self._instance(WINNER, ready=self.ready),
            self._instance(LOSER, ready=False, state="downloading"),
        ]

    def _instance(
        self, instance_id: str, *, ready: bool, state: str = "running"
    ) -> dict[str, Any]:
        return {
            "id": instance_id,
            "version": self.version,
            "state": state,
            "started": state == "running",
            "ready": ready,
            "pulling_progress": 55 if state == "downloading" else None,
        }

    def set_container_group_replicas(self, replicas: int) -> dict[str, Any]:
        self.events.append(f"replicas={replicas}")
        self.replicas = replicas
        return self.describe_container_group()

    def set_container_group_instance_deletion_cost(
        self, instance_id: str, deletion_cost: int
    ) -> dict[str, Any]:
        self.events.append(f"deletion_cost={instance_id}:{deletion_cost}")
        self.deletion_costs[instance_id] = deletion_cost
        return {}

    def start_container_group(self) -> None:
        self.events.append("start")
        self.status = "running"


def test_two_replica_race_protects_first_ready_then_scales_in() -> None:
    module = _module()
    clock = FakeClock()
    client = FakeRaceClient()
    result = module.race_to_one(
        client,
        group_name=GROUP,
        expected_image=IMAGE,
        timeout_seconds=120,
        poll_seconds=15,
        clock=clock.clock,
        sleep=clock.sleep,
        report=lambda _: None,
    )
    assert result["winner_id"] == WINNER
    assert result["replicas_before_job"] == 1
    assert result["instances_seen"] == 2
    assert client.events == [
        "replicas=2",
        "start",
        f"deletion_cost={LOSER}:0",
        f"deletion_cost={WINNER}:100000",
        "replicas=1",
    ]


def test_no_ready_instance_never_scales_in_or_submits() -> None:
    module = _module()
    clock = FakeClock()
    client = FakeRaceClient(ready=False)
    with pytest.raises(TimeoutError, match="two-replica"):
        module.race_to_one(
            client,
            group_name=GROUP,
            expected_image=IMAGE,
            timeout_seconds=120,
            poll_seconds=15,
            clock=clock.clock,
            sleep=clock.sleep,
            report=lambda _: None,
        )
    assert client.events == ["replicas=2", "start"]


def test_lost_winner_during_scale_in_fails_closed() -> None:
    module = _module()
    clock = FakeClock()
    client = FakeRaceClient(wrong_winner=True)
    with pytest.raises(TimeoutError, match="ONLY"):
        module.race_to_one(
            client,
            group_name=GROUP,
            expected_image=IMAGE,
            timeout_seconds=120,
            poll_seconds=15,
            clock=clock.clock,
            sleep=clock.sleep,
            report=lambda _: None,
        )
    assert client.deletion_costs[WINNER] == 100000
    assert client.replicas == 1


def test_wrong_image_rejected_before_capacity_mutation() -> None:
    module = _module()
    client = FakeRaceClient(image="docker.io/example/worker@sha256:" + "b" * 64)
    with pytest.raises(RuntimeError, match="pinned image"):
        module.race_to_one(
            client,
            group_name=GROUP,
            expected_image=IMAGE,
            timeout_seconds=120,
            report=lambda _: None,
        )
    assert client.events == []


def test_reset_requires_stopped_group_then_restores_one_replica() -> None:
    module = _module()
    clock = FakeClock()
    client = FakeRaceClient()
    client.replicas = 2
    module.reset_stopped_group(
        client,
        group_name=GROUP,
        expected_image=IMAGE,
        clock=clock.clock,
        sleep=clock.sleep,
    )
    assert client.events == ["replicas=1"]
    client.status = "running"
    with pytest.raises(RuntimeError, match="not fully stopped"):
        module.reset_stopped_group(
            client,
            group_name=GROUP,
            expected_image=IMAGE,
            clock=clock.clock,
            sleep=clock.sleep,
        )


def test_wrapper_race_is_opt_in_and_stops_before_restoring_replicas() -> None:
    source = Path("scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1").read_text(
        encoding="utf-8"
    )
    assert "[ValidateSet(1, 2)][int]$StartupReplicas = 1" in source
    assert "Two-GPU race requires -ExpectedPinnedImage" in source
    assert source.index("& $Python $RaceSelector @RaceArgs") < source.index(
        "& $Python $ReadyWait @ReadyArgs"
    )
    assert source.index("& $WorkerManager -Action Stop -Service ltx25") < source.index(
        "& $Python $RaceSelector --reset-stopped"
    )
    assert source.index("& $Python $ReadyWait @ReadyArgs") < source.index(
        "& $Python $Smoke @Arguments"
    )


def test_digest_validator_handles_letter_s_and_rejects_whitespace() -> None:
    module = _module()
    assert module._IMAGE_RE.fullmatch(
        "docker.io/example/services@sha256:" + "a" * 64
    )
    assert module._IMAGE_RE.fullmatch(
        "docker.io/example/my worker@sha256:" + "a" * 64
    ) is None
