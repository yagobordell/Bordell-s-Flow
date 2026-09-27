"""Race two independent one-replica LTX groups without a destructive scale-in.

The caller prepares both groups with the same immutable image and owns cleanup.
No Postgres job is submitted until the losing group is fully stopped and the
exact winning instance remains /ready twice. No deletion_cost is required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
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
_IMAGE_RE = re.compile(r"^[^\\s@]+@sha256:[0-9a-fA-F]{64}$")
_ACTIVE_STATES = frozenset({"allocating", "downloading", "creating", "running"})


class IsolatedClient(Protocol):
    def describe_container_group(self) -> dict[str, Any]: ...
    def list_container_group_instances(self) -> list[dict[str, Any]]: ...
    def start_container_group(self) -> None: ...
    def stop_container_group(self) -> None: ...


def _status(group: Mapping[str, Any]) -> str:
    current = group.get("current_state")
    return str(current.get("status", "")) if isinstance(current, dict) else ""


def _group(
    client: IsolatedClient, name: str, image: str, *, version: int | None = None
) -> dict[str, Any]:
    group = client.describe_container_group()
    probe = group.get("readiness_probe")
    http = probe.get("http") if isinstance(probe, dict) else None
    container = group.get("container")
    if (
        group.get("name") != name
        or not isinstance(container, dict)
        or container.get("image") != image
        or not isinstance(http, dict)
        or http.get("path") != "/ready"
        or type(group.get("version")) is not int
        or (version is not None and group["version"] != version)
        or group.get("replicas") != 1
    ):
        raise RuntimeError(f"LTX isolated race group {name} changed identity/configuration")
    return group


def _instances(client: IsolatedClient, version: int) -> list[dict[str, Any]]:
    instances = [
        item for item in client.list_container_group_instances()
        if item.get("version") == version and item.get("state") in _ACTIVE_STATES
    ]
    if len(instances) > 1:
        raise RuntimeError("One-replica race group exposed multiple active instances")
    return instances


def _ready_id(group: dict[str, Any], instances: list[dict[str, Any]]) -> str | None:
    if (
        _status(group) != "running"
        or group.get("pending_change") is not False
        or len(instances) != 1
    ):
        return None
    instance = instances[0]
    if (
        instance.get("state") == "running"
        and instance.get("started") is True
        and instance.get("ready") is True
        and isinstance(instance.get("id"), str)
        and instance["id"]
    ):
        return str(instance["id"])
    return None


def race(
    clients: list[tuple[str, IsolatedClient]],
    *,
    expected_image: str,
    timeout_seconds: float = 5400,
    poll_seconds: float = 15,
    stop_timeout_seconds: float = 300,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = print,
    assert_authority: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    if (
        len(clients) != 2
        or clients[0][0] == clients[1][0]
        or _IMAGE_RE.fullmatch(expected_image) is None
        or not 120 <= timeout_seconds <= 7200
        or not 1 <= poll_seconds <= 60
    ):
        raise ValueError("LTX isolated race requires two distinct pinned one-replica groups")
    started_at = clock()
    deadline = started_at + timeout_seconds
    versions: dict[str, int | None] = {}
    for name, client in clients:
        group = _group(client, name, expected_image)
        if _status(group) != "stopped" or group.get("pending_change") is not False:
            raise RuntimeError(f"LTX isolated race requires {name} stopped and settled")
        versions[name] = None  # Pin the current version only after start has settled.

    # Both PATCH-free starts happen before either worker is polled. The second
    # may remain unallocated indefinitely; a ready first worker can still win.
    for name, client in clients:
        assert_authority()
        client.start_container_group()
        report(f"LTX_ISOLATED started group={name} replicas=1")

    candidate: tuple[str, str] | None = None
    confirmations = 0
    last_report = float("-inf")
    seen: set[tuple[str, str]] = set()
    winner: tuple[str, str] | None = None
    while clock() < deadline:
        assert_authority()
        ready: list[tuple[str, str]] = []
        observations: list[str] = []
        for name, client in clients:
            group = _group(client, name, expected_image, version=versions[name])
            if versions[name] is None and group.get("pending_change") is False:
                versions[name] = group["version"]
            if _status(group) == "stopped":
                raise RuntimeError(f"LTX isolated race group {name} stopped unexpectedly")
            instances = _instances(client, versions[name]) if versions[name] is not None else []
            seen.update((name, str(item["id"])) for item in instances if item.get("id"))
            chosen = _ready_id(group, instances)
            if chosen is not None:
                ready.append((name, chosen))
            observations.append(
                f"{name}={_status(group)}:"
                f"{[(item.get('id'), item.get('state'), item.get('ready')) for item in instances]}"
            )
        selected = candidate if candidate in ready else (ready[0] if ready else None)
        if selected is None:
            candidate, confirmations = None, 0
        elif selected == candidate:
            confirmations += 1
        else:
            candidate, confirmations = selected, 1
        if clock() - last_report >= 60 or confirmations in (1, 2):
            report(
                f"LTX_ISOLATED elapsed_seconds={clock()-started_at:.1f} "
                f"groups={observations} candidate={candidate} confirmations={confirmations}/2"
            )
            last_report = clock()
        if confirmations >= 2 and candidate is not None:
            winner = candidate
            break
        sleep(min(poll_seconds, max(0.0, deadline - clock())))
    if winner is None:
        raise TimeoutError("Neither isolated LTX group passed /ready twice; no job submitted")

    winner_name, winner_id = winner
    loser_name, loser_client = next(
        (name, client) for name, client in clients if name != winner_name
    )
    winner_client = next(client for name, client in clients if name == winner_name)
    assert_authority()
    report(
        f"LTX_ISOLATED winner={winner_name}/{winner_id} "
        f"stopping_loser={loser_name} without scale-in"
    )
    loser_group = _group(loser_client, loser_name, expected_image, version=versions[loser_name])
    if _status(loser_group) != "stopped":
        loser_client.stop_container_group()

    stop_deadline = min(deadline, clock() + stop_timeout_seconds)
    winner_confirmations = 0
    while clock() < stop_deadline:
        assert_authority()
        loser_group = _group(
            loser_client, loser_name, expected_image, version=versions[loser_name]
        )
        loser_active = _instances(loser_client, versions[loser_name])
        # A group only counts as stopped after both control-plane settlement
        # and disappearance of its current-version active instance.
        loser_stopped = (
            _status(loser_group) == "stopped"
            and loser_group.get("pending_change") is False
            and not loser_active
        )
        winner_group = _group(
            winner_client, winner_name, expected_image, version=versions[winner_name]
        )
        winner_instances = _instances(winner_client, versions[winner_name])
        winner_still_ready = _ready_id(winner_group, winner_instances) == winner_id
        winner_confirmations = (
            winner_confirmations + 1 if loser_stopped and winner_still_ready else 0
        )
        if winner_confirmations >= 2:
            result = {
                "schema_version": 1,
                "strategy": "two_isolated_one_replica_groups_stop_loser",
                "winner_group": winner_name,
                "winner_id": winner_id,
                "loser_group": loser_name,
                "instances_seen": len(seen),
                "race_total_seconds": clock() - started_at,
                "replicas_before_job": 1,
                "loser_stopped": True,
            }
            report("LTX25_RACE_METRICS " + json.dumps(result, separators=(",", ":")))
            return result
        sleep(min(poll_seconds, max(0.0, stop_deadline - clock())))
    raise TimeoutError(
        "LTX isolated loser did not fully stop or winner changed; no job submitted"
    )


def _no_ltx_demand(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        row = connection.execute(
            """
            SELECT count(*) FROM gpu.jobs
            WHERE task IN ('video.ltx25.audio_to_video', 'video.ltx25.generate')
              AND status IN ('pending', 'retryable_failed', 'running')
            """
        ).fetchone()
    if row is None or int(row[0]) != 0:
        raise RuntimeError("Existing LTX Postgres demand forbids isolated race")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=5400)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()
    env_file = Path(args.env_file)
    if not env_file.is_absolute():
        env_file = _REPO_ROOT / env_file
    if not env_file.is_file():
        raise SystemExit("LTX isolated race requires a local .env")
    load_dotenv(env_file, override=False)
    manifest = json.loads(
        (_REPO_ROOT / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )
    service = manifest["services"]["ltx25"]
    stack = manifest["stack"]
    if stack["job_transport"] != "postgres" or service["capacity"]["start_replicas"] != 1:
        raise SystemExit("LTX isolated race requires single-replica Postgres worker manifest")
    if args.expected_image.rsplit("@", 1)[0] != service["image"].rsplit(":", 1)[0]:
        raise SystemExit("LTX isolated race image repository does not match manifest")
    api_key = os.getenv("SALAD_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("SALAD_API_KEY is required")
    names = [service["group_name"], service["group_name"] + "-race"]
    clients = [
        (
            name,
            SaladClient(
                SaladDispatchConfig(
                    api_key=api_key,
                    api_base_url=os.getenv(
                        "SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL
                    ).strip(),
                    organization_name=stack["organization"],
                    project_name=stack["project"],
                    container_group_name=name,
                )
            ),
        )
        for name in names
    ]
    try:
        with PostgresCapacityControllerOperationLock(
            resolve_capacity_controller_dsn()
        ) as authority:
            _no_ltx_demand(resolve_capacity_controller_dsn())
            result = race(
                clients,
                expected_image=args.expected_image,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                assert_authority=authority.assert_held,
            )
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output_json.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            temporary.replace(args.output_json)
    except Exception as error:
        print(f"LTX_ISOLATED_FAILED {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
