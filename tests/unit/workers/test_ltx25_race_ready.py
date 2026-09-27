from __future__ import annotations

import json
from typing import Any

import pytest

from ai_video_factory.workers.ltx25.race_ready import LTXRaceReadyGuard


class Registry:
    owner: Connection | None = None

    def connect(self, _dsn: str, *, autocommit: bool) -> Connection:
        assert autocommit is True
        return Connection(self)


class Connection:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.closed = False
        self.row: tuple[Any, ...] | None = None

    def execute(self, query: str, params: tuple[int, int]) -> Connection:
        assert len(params) == 2
        if "pg_try_advisory_lock" in query:
            acquired = self.registry.owner in (None, self)
            if acquired:
                self.registry.owner = self
            self.row = (acquired,)
        elif "FROM pg_locks" in query:
            self.row = (1,) if self.registry.owner is self else None
        else:
            raise AssertionError(f"unexpected SQL in guard: {query}")
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        self.closed = True
        if self.registry.owner is self:
            self.registry.owner = None


def _transport(state: dict[str, Any]):
    def request(method: str, payload: bytes | None = None) -> tuple[int, bytes]:
        if method == "GET":
            return 200, json.dumps({"deletion_cost": state["cost"]}).encode("utf-8")
        assert method == "PUT"
        assert payload is not None
        state["puts"].append(json.loads(payload))
        if state.get("persist_put", True):
            state["cost"] = json.loads(payload)["deletion_cost"]
        return 204, b""

    return request


def test_only_first_ready_instance_claims_lock_and_owns_imds_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry()
    leader = LTXRaceReadyGuard("postgres://fake", connect=registry.connect)
    loser = LTXRaceReadyGuard("postgres://fake", connect=registry.connect)
    leader_state: dict[str, Any] = {"cost": 0, "puts": []}
    loser_state: dict[str, Any] = {"cost": 0, "puts": []}
    monkeypatch.setattr(leader, "_request", _transport(leader_state))
    monkeypatch.setattr(loser, "_request", _transport(loser_state))

    leader.ensure_protected()
    leader.ensure_protected()
    assert leader_state["cost"] == 100000
    assert leader_state["puts"] == [{"deletion_cost": 100000}]
    with pytest.raises(RuntimeError, match="another LTX worker became ready first"):
        loser.ensure_protected()
    assert loser_state["cost"] == 0
    assert loser_state["puts"] == []

    leader.close()
    loser.ensure_protected()
    assert loser_state["cost"] == 100000
    loser.close()


def test_ready_fails_closed_if_imds_does_not_persist_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry()
    guard = LTXRaceReadyGuard("postgres://fake", connect=registry.connect)
    state: dict[str, Any] = {"cost": 0, "puts": [], "persist_put": False}
    monkeypatch.setattr(guard, "_request", _transport(state))
    with pytest.raises(RuntimeError, match="did not persist"):
        guard.ensure_protected()
    assert registry.owner is None
    assert state["cost"] == 0
    assert state["puts"] == [
        {"deletion_cost": 100000},
        {"deletion_cost": 0},
    ]
    guard.close()


def test_ready_fails_closed_if_election_lock_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry()
    guard = LTXRaceReadyGuard("postgres://fake", connect=registry.connect)
    state: dict[str, Any] = {"cost": 0, "puts": []}
    monkeypatch.setattr(guard, "_request", _transport(state))
    guard.ensure_protected()
    registry.owner = None
    with pytest.raises(RuntimeError, match="no longer owns"):
        guard.ensure_protected()
    assert state["cost"] == 0
    assert state["puts"] == [
        {"deletion_cost": 100000},
        {"deletion_cost": 0},
    ]
    assert guard._connection is None


def test_local_imds_uses_direct_metadata_only_and_checked_http_protocol() -> None:
    from pathlib import Path

    source = Path("src/ai_video_factory/workers/ltx25/race_ready.py").read_text(
        encoding="utf-8"
    )
    assert "http://169.254.169.254/v1/deletion-cost" in source
    assert '"Metadata": "true"' in source
    assert "ProxyHandler({})" in source
    assert "status != 200" in source
    assert "status != 204" in source
    assert "pg_try_advisory_lock" in source
    assert "set_container_group_instance_deletion_cost" not in source


def test_opt_in_is_pinned_to_reviewed_monje_image() -> None:
    from pathlib import Path

    manifest = Path("deploy/salad/services.json").read_text(encoding="utf-8")
    runtime = Path("src/ai_video_factory/workers/ltx25/runtime.py").read_text(
        encoding="utf-8"
    )
    smoke = Path("scripts/smoke/run_ltx25_stage2_bootstrap_retry.ps1").read_text(
        encoding="utf-8"
    )
    assert '"LTX_RACE_PROTECT_READY_WITH_IMDS": "true"' in manifest
    assert "readiness_hook=" in runtime
    assert "-UseImdsReadyProtection" in smoke
    assert "-StartupReplicas 2" in smoke
