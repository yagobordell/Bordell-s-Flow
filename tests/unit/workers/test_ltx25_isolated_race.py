from __future__ import annotations

from typing import Any

import pytest

from scripts.smoke.start_ltx25_isolated_race import race

IMAGE = "docker.io/example/ltx@sha256:" + "a" * 64
MAIN = "ai-video-factory-ltx25-worker-v3"
AUX = MAIN + "-race"


class Clock:
    now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Group:
    def __init__(
        self,
        name: str,
        *,
        ready_at: float = 0.0,
        allocated: bool = True,
        downloading: bool = False,
        stop_settles: bool = True,
        ready_lost_after_stop: bool = False,
        clock: Clock,
    ) -> None:
        self.name = name
        self.clock = clock
        self.ready_at = ready_at
        self.allocated = allocated
        self.downloading = downloading
        self.stop_settles = stop_settles
        self.ready_lost_after_stop = ready_lost_after_stop
        self.status = "stopped"
        self.pending = False
        self.version = 42
        self.id = name + "-node"
        self.events: list[str] = []
        self.other: Group | None = None

    def describe_container_group(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "replicas": 1,
            "pending_change": self.pending,
            "current_state": {"status": self.status},
            "container": {"image": IMAGE},
            "readiness_probe": {"http": {"path": "/ready"}},
        }

    def list_container_group_instances(self) -> list[dict[str, Any]]:
        if self.status == "stopped" or not self.allocated:
            return []
        ready = self.clock.now >= self.ready_at
        if self.ready_lost_after_stop and self.other is not None:
            if "stop" in self.other.events:
                ready = False
        return [{
            "id": self.id,
            "version": self.version,
            "state": "downloading" if self.downloading else "running",
            "started": not self.downloading,
            "ready": ready,
        }]

    def start_container_group(self) -> None:
        self.events.append("start")
        self.status = "running"

    def stop_container_group(self) -> None:
        self.events.append("stop")
        if self.stop_settles:
            self.status = "stopped"


@pytest.mark.parametrize("winner_is_main", [True, False])
def test_one_ready_one_never_allocated_stops_loser_not_winner(
    winner_is_main: bool,
) -> None:
    clock = Clock()
    main = Group(MAIN, allocated=winner_is_main, clock=clock)
    aux = Group(AUX, allocated=not winner_is_main, clock=clock)
    winner = main if winner_is_main else aux
    loser = aux if winner_is_main else main
    result = race(
        [(MAIN, main), (AUX, aux)],
        expected_image=IMAGE,
        timeout_seconds=120,
        poll_seconds=15,
        clock=clock.clock,
        sleep=clock.sleep,
        report=lambda _: None,
    )
    assert result["winner_group"] == winner.name
    assert result["winner_id"] == winner.id
    assert result["instances_seen"] == 1
    assert result["loser_stopped"] is True
    assert winner.events == ["start"]
    assert loser.events == ["start", "stop"]
    assert winner.status == "running"
    assert loser.status == "stopped"


def test_ready_first_wins_even_if_second_is_downloading() -> None:
    clock = Clock()
    main = Group(MAIN, clock=clock, ready_at=30)
    aux = Group(AUX, clock=clock, downloading=True)
    result = race(
        [(MAIN, main), (AUX, aux)],
        expected_image=IMAGE,
        timeout_seconds=120,
        poll_seconds=15,
        clock=clock.clock,
        sleep=clock.sleep,
        report=lambda _: None,
    )
    assert result["winner_group"] == MAIN
    assert aux.events == ["start", "stop"]
    assert main.status == "running"


def test_loser_must_be_fully_stopped_before_winner_can_be_returned() -> None:
    clock = Clock()
    main = Group(MAIN, clock=clock)
    aux = Group(AUX, allocated=False, stop_settles=False, clock=clock)
    with pytest.raises(TimeoutError, match="loser did not fully stop"):
        race(
            [(MAIN, main), (AUX, aux)],
            expected_image=IMAGE,
            timeout_seconds=120,
            stop_timeout_seconds=30,
            poll_seconds=15,
            clock=clock.clock,
            sleep=clock.sleep,
            report=lambda _: None,
        )
    assert main.events == ["start"]
    assert aux.events == ["start", "stop"]


def test_lost_winner_after_loser_stop_fails_closed() -> None:
    clock = Clock()
    main = Group(MAIN, clock=clock, ready_lost_after_stop=True)
    aux = Group(AUX, clock=clock, allocated=False)
    main.other = aux
    with pytest.raises(TimeoutError, match="winner changed"):
        race(
            [(MAIN, main), (AUX, aux)],
            expected_image=IMAGE,
            timeout_seconds=120,
            stop_timeout_seconds=30,
            poll_seconds=15,
            clock=clock.clock,
            sleep=clock.sleep,
            report=lambda _: None,
        )
    assert aux.events == ["start", "stop"]


def test_live_group_is_never_started_or_mutated() -> None:
    clock = Clock()
    main = Group(MAIN, clock=clock)
    aux = Group(AUX, clock=clock)
    aux.status = "running"
    with pytest.raises(RuntimeError, match="stopped and settled"):
        race(
            [(MAIN, main), (AUX, aux)],
            expected_image=IMAGE,
            timeout_seconds=120,
            clock=clock.clock,
            sleep=clock.sleep,
        )
    assert main.events == []
    assert aux.events == []


def test_no_ready_group_never_returns_winner() -> None:
    clock = Clock()
    main = Group(MAIN, ready_at=999, clock=clock)
    aux = Group(AUX, allocated=False, clock=clock)
    with pytest.raises(TimeoutError, match="Neither isolated"):
        race(
            [(MAIN, main), (AUX, aux)],
            expected_image=IMAGE,
            timeout_seconds=120,
            poll_seconds=15,
            clock=clock.clock,
            sleep=clock.sleep,
            report=lambda _: None,
        )
    assert main.events == ["start"]
    assert aux.events == ["start"]


def test_wrapper_stops_both_groups_and_does_not_use_deletion_cost() -> None:
    from pathlib import Path

    manager = Path("scripts/salad/manage_salad_worker.ps1").read_text(encoding="utf-8")
    controlled = Path("scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1").read_text(
        encoding="utf-8"
    )
    guarded = Path("scripts/smoke/run_ltx25_stage2_bootstrap_retry.ps1").read_text(
        encoding="utf-8"
    )
    selector = Path("scripts/smoke/start_ltx25_isolated_race.py").read_text(
        encoding="utf-8"
    )

    assert "$GroupName = \"$GroupName-race\"" in manager
    assert "-IsolatedRaceGroup" in controlled
    assert "-IsolatedRaceGroup" in guarded
    assert 'start_ltx25_isolated_race.py' in controlled
    assert 'ltx25_race_winner.json' in controlled
    assert '"--expected-instance"' in controlled
    assert 'start_container_group_instance' not in selector
    assert 'set_container_group_replicas' not in selector
    assert 'set_container_group_instance_deletion_cost' not in selector
    assert '[ValidateSet(1, 2)][int]$StartupReplicas = 2' in guarded
