"""Opt-in, bounded 2-to-1 LTX bootstrap race; never submits a Postgres job.

A single group briefly requests two current-version RTX 5090 instances. Only
after two observations of the same ready instance does it protect that instance
with Salad deletion_cost, scale to one, and verify that the winner survived.
The controlled smoke wrapper owns the unconditional stop on every exit path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

from ai_video_factory.inference.capacity_controller import (
    PostgresCapacityControllerOperationLock,
    resolve_capacity_controller_dsn,
    validate_capacity_controller_dsn,
)
from ai_video_factory.inference.salad import (
    DEFAULT_SALAD_API_BASE_URL,
    SaladClient,
    SaladDispatchConfig,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-fA-F]{64}$")
_ACTIVE_STATES = frozenset({"allocating", "downloading", "creating", "running"})


class RaceClient(Protocol):
    def describe_container_group(self) -> dict[str, Any]: ...
    def list_container_group_instances(self) -> list[dict[str, Any]]: ...
    def get_container_group_instance(self, instance_id: str) -> dict[str, Any]: ...
    def set_container_group_replicas(self, replicas: int) -> dict[str, Any]: ...
    def set_container_group_instance_deletion_cost(
        self, instance_id: str, deletion_cost: int
    ) -> dict[str, Any]: ...
    def start_container_group(self) -> None: ...


def _status(group: dict[str, Any]) -> str:
    state = group.get("current_state")
    return str(state.get("status", "")) if isinstance(state, dict) else ""


def _verify_group(group: dict[str, Any], *, name: str, image: str) -> None:
    if group.get("name") != name:
        raise RuntimeError("Salad returned another container group")
    container = group.get("container")
    actual = container.get("image") if isinstance(container, dict) else None
    if actual != image or _IMAGE_RE.fullmatch(str(actual or "")) is None:
        raise RuntimeError("Salad group is not running the reviewed pinned image")
    probe = group.get("readiness_probe")
    probe_http = probe.get("http") if isinstance(probe, dict) else None
    if not isinstance(probe_http, dict) or probe_http.get("path") != "/ready":
        raise RuntimeError("Salad group must use HTTP /ready")
    if not isinstance(group.get("version"), int) or isinstance(group["version"], bool):
        raise RuntimeError("Salad group does not expose a valid version")


def race_to_one(
    client: RaceClient,
    *,
    group_name: str,
    expected_image: str,
    timeout_seconds: float = 2400.0,
    poll_seconds: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = print,
    assert_authority: Callable[[], None] = lambda: None,
    worker_self_protection: bool = False,
) -> dict[str, Any]:
    """Return only when the FIRST ready instance is the SOLE running replica."""
    if not 120 <= timeout_seconds <= 7200 or not 1 <= poll_seconds <= 60:
        raise ValueError("race timeout/poll must be bounded")
    if _IMAGE_RE.fullmatch(expected_image) is None:
        raise ValueError("race requires an immutable --expected-image")

    started = clock()
    deadline = started + timeout_seconds
    group = client.describe_container_group()
    _verify_group(group, name=group_name, image=expected_image)
    if _status(group) != "stopped" or group.get("pending_change") is not False:
        raise RuntimeError(
            "LTX race requires a settled stopped group; refusing to touch live workers"
        )
    if group.get("replicas") != 1:
        raise RuntimeError("LTX race requires the existing stopped group to have replicas=1")
    assert_authority()
    report("LTX_RACE requesting two replicas; no Postgres job may be submitted yet")
    client.set_container_group_replicas(2)

    # Wait for the capacity update before starting; never layer two pending writes.
    while clock() < deadline:
        assert_authority()
        group = client.describe_container_group()
        _verify_group(group, name=group_name, image=expected_image)
        if group.get("pending_change") is False and group.get("replicas") == 2:
            if _status(group) != "stopped":
                raise RuntimeError("LTX group started externally during race setup")
            break
        sleep(min(poll_seconds, max(0.0, deadline - clock())))
    else:
        raise TimeoutError("LTX two-replica configuration did not settle")

    assert_authority()
    client.start_container_group()
    version = None
    candidate = None
    consecutive_ready = 0
    first_started_seconds = None
    first_ready_seconds = None
    seen: set[str] = set()
    ever_ready: set[str] = set()
    last_state = ""
    last_report = float("-inf")
    while clock() < deadline:
        assert_authority()
        group = client.describe_container_group()
        _verify_group(group, name=group_name, image=expected_image)
        if group.get("replicas") != 2:
            raise RuntimeError("LTX race capacity was modified externally")
        if _status(group) == "stopped":
            raise RuntimeError("LTX group stopped before race winner was selected")
        if version is None and group.get("pending_change") is False:
            version = group["version"]
        if version is not None and group["version"] != version:
            raise RuntimeError("LTX group version changed during race")
        instances = client.list_container_group_instances()
        current = [
            item for item in instances
            if (version is None or item.get("version") == version)
            and item.get("state") in _ACTIVE_STATES
        ]
        if len(current) > 2:
            raise RuntimeError("LTX race found more than two current active instances")
        seen.update(str(item.get("id")) for item in current if item.get("id"))
        started_instances = [
            item for item in current
            if item.get("state") == "running" and item.get("started") is True
        ]
        if first_started_seconds is None and started_instances:
            first_started_seconds = clock() - started
        ready = [
            item for item in started_instances
            if item.get("ready") is True and item.get("version") == version
        ]
        ready_ids = sorted(str(item["id"]) for item in ready if item.get("id"))
        ever_ready.update(ready_ids)
        ready_id = candidate if candidate in ready_ids else (ready_ids[0] if ready_ids else None)
        if ready_id is None:
            candidate, consecutive_ready = None, 0
        elif ready_id == candidate:
            consecutive_ready += 1
        else:
            candidate, consecutive_ready = ready_id, 1
            if first_ready_seconds is None:
                first_ready_seconds = clock() - started
        states = [
            (item.get("id"), item.get("state"), item.get("ready"), item.get("pulling_progress"))
            for item in current
        ]
        observation = (
            f"version={version} group={_status(group)} pending={group.get('pending_change')} "
            f"instances={states} candidate={candidate} confirmations={consecutive_ready}/2"
        )
        if observation != last_state or clock() - last_report >= 60:
            report(f"LTX_RACE elapsed_seconds={clock()-started:.1f} {observation}")
            last_state, last_report = observation, clock()
        if (
            consecutive_ready >= 2
            and candidate is not None
            and group.get("pending_change") is False
        ):
            break
        sleep(min(poll_seconds, max(0.0, deadline - clock())))
    else:
        raise TimeoutError("No two-replica LTX instance passed two /ready observations")

    assert_authority()
    group = client.describe_container_group()
    current = client.list_container_group_instances()
    _verify_group(group, name=group_name, image=expected_image)
    if (
        group.get("pending_change") is not False
        or group.get("replicas") != 2
        or group.get("version") != version
        or not any(
            item.get("id") == candidate
            and item.get("state") == "running"
            and item.get("started") is True
            and item.get("ready") is True
            and item.get("version") == version
            for item in current
        )
    ):
        raise RuntimeError("LTX race winner changed before scale-in; refusing job submission")

    # A requested second replica is not necessarily an allocated instance.
    # In the repeatedly observed cold-start case, only the ready winner exists.
    # There is no loser to protect against: do not require a deletion-cost PATCH
    # whose readback Salad has repeatedly failed to persist.
    sole_confirmations = 0
    for _ in range(3):
        assert_authority()
        sole_group = client.describe_container_group()
        _verify_group(sole_group, name=group_name, image=expected_image)
        sole_active = [
            item for item in client.list_container_group_instances()
            if item.get("version") == version and item.get("state") in _ACTIVE_STATES
        ]
        if (
            sole_group.get("version") == version
            and sole_group.get("replicas") == 2
            and sole_group.get("pending_change") is False
            and _status(sole_group) == "running"
            and len(sole_active) == 1
            and sole_active[0].get("id") == candidate
            and sole_active[0].get("state") == "running"
            and sole_active[0].get("started") is True
            and sole_active[0].get("ready") is True
        ):
            sole_confirmations += 1
        else:
            break
    sole_ready = sole_confirmations == 3
    if sole_ready:
        report(
            "LTX_RACE one_active_winner "
            f"winner={candidate} confirmations=3/3 "
            "configured_replicas=2 allocated_loser=none"
        )
        report(
            f"LTX_RACE winner={candidate}; scaling 2 -> 1 with no "
            "allocated competitor; no deletion-cost PATCH required"
        )
        assert_authority()
    elif worker_self_protection:
        # A reviewed worker's /ready only succeeds after owning the first-ready
        # Postgres election lock and verifying its OWN IMDS deletion cost.
        # Never mix that local writer with external PATCH.
        guard_deadline = min(deadline, clock() + 90)
        guard_confirmations = 0
        last_loser_id: str | None = None
        while clock() < guard_deadline:
            assert_authority()
            protected_group = client.describe_container_group()
            _verify_group(protected_group, name=group_name, image=expected_image)
            active = [
                item for item in client.list_container_group_instances()
                if item.get("version") == version and item.get("state") in _ACTIVE_STATES
            ]
            selected = next((i for i in active if i.get("id") == candidate), None)
            rivals = [i for i in active if i.get("id") != candidate]
            if (
                protected_group.get("version") != version
                or protected_group.get("replicas") != 2
                or protected_group.get("pending_change") is not False
                or _status(protected_group) != "running"
                or selected is None
                or selected.get("state") != "running"
                or selected.get("started") is not True
                or selected.get("ready") is not True
                or len(rivals) > 1
            ):
                raise RuntimeError(
                    "LTX race lost IMDS-protected winner or group version; "
                    "no job submitted"
                )
            rival_id = str(rivals[0]["id"]) if rivals else None
            if rivals:
                if rivals[0].get("ready") is True or rival_id in ever_ready:
                    raise RuntimeError(
                        "LTX race found a second ready worker; refusing unsafe scale-in"
                    )
                detail = client.get_container_group_instance(rival_id)
                if (
                    detail.get("id") != rival_id
                    or detail.get("version") != version
                    or detail.get("deletion_cost", 0) != 0
                ):
                    raise RuntimeError(
                        "LTX race loser has unconfirmed deletion cost; no scale-in"
                    )
            if guard_confirmations and rival_id != last_loser_id:
                guard_confirmations = 0
            last_loser_id = rival_id
            guard_confirmations += 1
            if guard_confirmations >= 2:
                break
            sleep(min(poll_seconds, max(0.0, guard_deadline - clock())))
        else:
            raise TimeoutError("LTX race could not confirm IMDS-protected first-ready worker")
        report(
            "LTX_RACE imds_first_ready_verified "
            f"winner={candidate} loser={last_loser_id or 'none'} confirmations=2/2"
        )
        assert_authority()
        report(
            f"LTX_RACE winner={candidate}; scaling 2 -> 1 using local IMDS "
            "protection and no external deletion-cost PATCH"
        )
    else:
        # Legacy direct-selector mode: never trust PATCH acknowledgement without
        # exact-instance GET confirming its deletion-cost protection.
        # Only configured capacity is two. Under supply pressure Salad may have
        # allocated just the one healthy winner. Treat zero active competitors as
        # a valid, explicitly checked fallback, never as proof of two live GPUs.
        # Record every competitor whose deletion cost we explicitly set.
        protected_loser_ids: set[str] = set()
        for item in current:
            if item.get("version") == version and item.get("id") != candidate:
                if item.get("state") in _ACTIVE_STATES:
                    assert_authority()
                    loser_id = str(item["id"])
                    client.set_container_group_instance_deletion_cost(loser_id, 0)
                    protected_loser_ids.add(loser_id)
        assert_authority()
        winner_patch = client.set_container_group_instance_deletion_cost(candidate, 100000)
        if winner_patch.get("id") not in (None, candidate):
            raise RuntimeError("Salad acknowledged deletion cost for a different winner")
        report(
            "LTX_RACE deletion_cost_patch "
            f"winner={candidate} acknowledged={winner_patch.get('deletion_cost')!r}"
        )

        # Read exact instances twice; PATCH acknowledgement is not proof of
        # application. Also require a stable current-version instance topology.
        protection_deadline = min(deadline, clock() + 90)
        protection_confirmations = 0
        confirmed_loser_id: str | None = None
        while clock() < protection_deadline:
            assert_authority()
            protected_group = client.describe_container_group()
            _verify_group(protected_group, name=group_name, image=expected_image)
            if (
                protected_group.get("version") != version
                or protected_group.get("replicas") != 2
                or protected_group.get("pending_change") is not False
                or _status(protected_group) != "running"
            ):
                raise RuntimeError("LTX race changed before deletion costs were confirmed")
            protected_instances = [
                item for item in client.list_container_group_instances()
                if item.get("version") == version and item.get("state") in _ACTIVE_STATES
            ]
            selected = next(
                (item for item in protected_instances if item.get("id") == candidate), None
            )
            others = [item for item in protected_instances if item.get("id") != candidate]
            if (
                selected is None
                or selected.get("state") != "running"
                or selected.get("started") is not True
                or selected.get("ready") is not True
                or len(others) > 1
            ):
                raise RuntimeError(
                    "LTX race ready winner or current-version topology changed "
                    "before deletion cost confirmation; no job submitted"
                )
            loser_id = str(others[0]["id"]) if others else None
            if loser_id is not None and loser_id not in protected_loser_ids:
                # Another replica appeared while the winner was bootstrapping.
                # Do not assume that its initial deletion cost is zero.
                assert_authority()
                client.set_container_group_instance_deletion_cost(loser_id, 0)
                protected_loser_ids.add(loser_id)
                protection_confirmations = 0
                confirmed_loser_id = None
                report(f"LTX_RACE late_competitor_protected loser={loser_id}")
                sleep(min(poll_seconds, max(0.0, protection_deadline - clock())))
                continue

            winner_detail = client.get_container_group_instance(candidate)
            loser_detail = (
                client.get_container_group_instance(loser_id) if loser_id is not None else None
            )
            winner_cost = winner_detail.get("deletion_cost")
            loser_cost = loser_detail.get("deletion_cost") if loser_detail is not None else None
            if (
                winner_detail.get("id") != candidate
                or winner_detail.get("version") != version
                or (loser_detail is not None and (
                    loser_detail.get("id") != loser_id
                    or loser_detail.get("version") != version
                ))
            ):
                raise RuntimeError(
                    "LTX race exact-instance readback changed identity/version; "
                    "no job submitted"
                )
            report(
                "LTX_RACE deletion_cost_readback "
                f"winner={candidate} detail={winner_cost!r} "
                f"list={selected.get('deletion_cost')!r} "
                f"loser={loser_id or 'none'} detail={loser_cost!r} "
                f"list={others[0].get('deletion_cost') if others else None!r}"
            )
            if (
                winner_cost == 100000
                and (loser_id is None or loser_cost == 0)
            ):
                if protection_confirmations and loser_id != confirmed_loser_id:
                    protection_confirmations = 0
                confirmed_loser_id = loser_id
                protection_confirmations += 1
                if protection_confirmations >= 2:
                    break
            else:
                protection_confirmations = 0
                confirmed_loser_id = None
            sleep(min(poll_seconds, max(0.0, protection_deadline - clock())))
        else:
            raise TimeoutError(
                "LTX race deletion costs were not confirmed before scale-in; no job submitted"
            )

        # A new competitor can be assigned after a valid cost readback. Fail
        # closed if the instance topology changes between confirmation and PATCH.
        assert_authority()
        final_group = client.describe_container_group()
        _verify_group(final_group, name=group_name, image=expected_image)
        final_active = [
            item for item in client.list_container_group_instances()
            if item.get("version") == version and item.get("state") in _ACTIVE_STATES
        ]
        expected_ids = {candidate}
        if confirmed_loser_id is not None:
            expected_ids.add(confirmed_loser_id)
        if (
            final_group.get("version") != version
            or final_group.get("replicas") != 2
            or final_group.get("pending_change") is not False
            or _status(final_group) != "running"
            or {str(item.get("id")) for item in final_active} != expected_ids
            or not any(
                item.get("id") == candidate
                and item.get("state") == "running"
                and item.get("started") is True
                and item.get("ready") is True
                for item in final_active
            )
        ):
            raise RuntimeError(
                "LTX race topology changed after cost readback; no job submitted"
            )
        report(
            "LTX_RACE deletion_costs_verified "
            f"winner={candidate} loser={confirmed_loser_id or 'none'} confirmations=2/2"
        )
        assert_authority()
        report(f"LTX_RACE winner={candidate}; scaling 2 -> 1 before submitting any job")
    client.set_container_group_replicas(1)
    scale_started = clock()

    while clock() < deadline:
        assert_authority()
        group = client.describe_container_group()
        _verify_group(group, name=group_name, image=expected_image)
        if group.get("version") != version:
            raise RuntimeError("LTX race version changed during scale-in")
        instances = client.list_container_group_instances()
        active = [
            item for item in instances
            if item.get("state") in _ACTIVE_STATES and item.get("version") == version
        ]
        if group.get("replicas") == 1 and group.get("pending_change") is False:
            # Fail closed if scale-in removed the protected winner.
            # A replacement worker does not inherit race winner identity.
            if not any(item.get("id") == candidate for item in active):
                survivor_ids = sorted(str(item.get("id")) for item in active)
                raise RuntimeError(
                    "LTX race lost the protected winner during scale-in; "
                    f"winner={candidate} active={survivor_ids}; no job submitted"
                )
        if (
            group.get("replicas") == 1
            and group.get("pending_change") is False
            and len(active) == 1
            and active[0].get("id") == candidate
            and active[0].get("state") == "running"
            and active[0].get("started") is True
            and active[0].get("ready") is True
        ):
            result = {
                "schema_version": 1,
                "strategy": "two_replicas_first_ready_then_scale_to_one",
                "winner_id": candidate,
                "instances_seen": len(seen),
                "first_started_seconds": first_started_seconds,
                "first_ready_seconds": first_ready_seconds,
                "scale_in_seconds": clock() - scale_started,
                "race_total_seconds": clock() - started,
                "replicas_before_job": 1,
            }
            report("LTX25_RACE_METRICS " + json.dumps(result, separators=(",", ":")))
            return result
        sleep(min(poll_seconds, max(0.0, deadline - clock())))
    raise TimeoutError("LTX race could not confirm ONLY the chosen ready instance after scale-in")


def reset_stopped_group(
    client: RaceClient, *, group_name: str, expected_image: str,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    assert_authority: Callable[[], None] = lambda: None,
) -> None:
    """Restore the configured count, but NEVER act on a running group."""
    deadline = clock() + 180
    while clock() < deadline:
        assert_authority()
        group = client.describe_container_group()
        _verify_group(group, name=group_name, image=expected_image)
        if _status(group) == "stopped" and group.get("pending_change") is False:
            break
        sleep(5)
    else:
        raise RuntimeError("LTX group is not fully stopped; refusing replica reset")
    if group.get("replicas") != 1:
        assert_authority()
        client.set_container_group_replicas(1)
        while clock() < deadline:
            assert_authority()
            group = client.describe_container_group()
            _verify_group(group, name=group_name, image=expected_image)
            if (
                _status(group) == "stopped"
                and group.get("pending_change") is False
                and group.get("replicas") == 1
            ):
                break
            sleep(5)
        else:
            raise TimeoutError("Stopped LTX group did not return to replicas=1")
    print("LTX_RACE_RESET stopped=true configured_replicas=1")


def _assert_no_existing_ltx_demand(dsn: str) -> None:
    """Never start two Postgres pollers while older LTX work is claimable."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        row = connection.execute(
            """
            SELECT count(*)
            FROM gpu.jobs
            WHERE task IN ('video.ltx25.audio_to_video', 'video.ltx25.generate')
              AND status IN ('pending', 'retryable_failed', 'running')
            """
        ).fetchone()
    if row is None or int(row[0]) != 0:
        raise RuntimeError(
            "Existing LTX Postgres demand must be resolved before racing two workers"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=2400)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--reset-stopped", action="store_true")
    parser.add_argument("--ready-protected-by-imds", action="store_true")
    args = parser.parse_args()
    env_file = Path(args.env_file)
    if not env_file.is_absolute():
        env_file = _REPO_ROOT / env_file
    if not env_file.is_file():
        raise SystemExit("LTX race requires the local .env")
    load_dotenv(env_file, override=False)
    manifest = json.loads(
        (_REPO_ROOT / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )
    if manifest["stack"]["job_transport"] != "postgres":
        raise SystemExit("LTX race requires canonical Postgres transport")
    service = manifest["services"]["ltx25"]
    if args.ready_protected_by_imds and str(service["environment"].get(
        "LTX_RACE_PROTECT_READY_WITH_IMDS", ""
    )).lower() != "true":
        raise SystemExit("IMDS race requires a reviewed protected worker manifest")
    if args.ready_protected_by_imds:
        # A session-scoped election cannot work through Supabase's known
        # transaction-pooling port; reject before requesting paid GPUs.
        validate_capacity_controller_dsn(os.getenv("POSTGRES_DSN", ""))
    if service["capacity"]["start_replicas"] != 1:
        raise SystemExit("LTX race requires manifest start_replicas=1")
    if args.expected_image.rsplit("@", 1)[0] != service["image"].rsplit(":", 1)[0]:
        raise SystemExit("Pinned race image repository differs from manifest")
    api_key = os.getenv("SALAD_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("SALAD_API_KEY is required for LTX race")
    client = SaladClient(
        SaladDispatchConfig(
            api_key=api_key,
            api_base_url=os.getenv("SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL),
            organization_name=manifest["stack"]["organization"],
            project_name=manifest["stack"]["project"],
            container_group_name=service["group_name"],
        )
    )
    try:
        with PostgresCapacityControllerOperationLock(
            resolve_capacity_controller_dsn()
        ) as authority:
            if args.reset_stopped:
                reset_stopped_group(
                    client, group_name=service["group_name"],
                    expected_image=args.expected_image,
                    assert_authority=authority.assert_held,
                )
            else:
                _assert_no_existing_ltx_demand(resolve_capacity_controller_dsn())
                race_to_one(
                    client, group_name=service["group_name"],
                    expected_image=args.expected_image,
                    timeout_seconds=args.timeout_seconds,
                    poll_seconds=args.poll_seconds,
                    assert_authority=authority.assert_held,
                    worker_self_protection=args.ready_protected_by_imds,
                )
    except Exception as error:
        print(f"LTX_RACE_FAILED {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
