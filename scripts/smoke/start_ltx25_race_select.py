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
) -> dict[str, Any]:
    """Return only when the FIRST ready instance is the SOLE running replica."""
    if not 120 <= timeout_seconds <= 3600 or not 1 <= poll_seconds <= 60:
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

    # Salad's documented scale-in selects LOWEST deletion_cost first.
    for item in current:
        if item.get("version") == version and item.get("id") != candidate:
            if item.get("state") in _ACTIVE_STATES:
                assert_authority()
                client.set_container_group_instance_deletion_cost(str(item["id"]), 0)
    assert_authority()
    client.set_container_group_instance_deletion_cost(candidate, 100000)
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
                )
    except Exception as error:
        print(f"LTX_RACE_FAILED {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
