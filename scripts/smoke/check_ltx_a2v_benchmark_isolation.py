"""Read-only A2V experiment gate: production may run on its separate task.

Prepare the separate group first; its own Salad group must be stopped and
quiescent. Production A2V can run because the job task names are isolated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ai_video_factory.inference.salad import (
    DEFAULT_SALAD_API_BASE_URL,
    SaladClient,
    SaladDispatchConfig,
)

_ROOT = Path(__file__).resolve().parents[2]


def assert_quiescent_group(
    group: Mapping[str, Any], *, expected_group_name: str,
    instances: list[Mapping[str, Any]] | None = None,
) -> None:
    if group.get("name") != expected_group_name:
        raise RuntimeError("Salad returned an unexpected A2V group")
    state = group.get("current_state")
    status = state.get("status") if isinstance(state, dict) else None
    if (
        status != "stopped"
        or group.get("pending_change") is not False
        or any(
            instance.get("state") in {"running", "allocating", "downloading", "creating"}
            for instance in (instances or [])
        )
    ):
        raise RuntimeError(
            f"Refusing A2V benchmark: {expected_group_name} is not quiescent "
            f"(status={status}, replicas={group.get('replicas')}, "
            f"pending={group.get('pending_change')})"
        )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name} for A2V benchmark isolation preflight")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--reuse-production", action="store_true")
    parser.add_argument("--expected-production-image", default="")
    parser.add_argument(
        "--expected-task-mode", choices=("a2v", "a2v_experimental"), default="a2v"
    )
    args = parser.parse_args()
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = _ROOT / env_path
    if not env_path.is_file():
        raise SystemExit(f"Missing Salad env file: {env_path}")
    load_dotenv(_ROOT / "config" / "salad.env", override=False)
    load_dotenv(env_path, override=False)
    manifest = json.loads(
        (_ROOT / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )
    services = manifest["services"]
    production = services["ltx25"]
    experiment = services["ltx25_a2v_experimental"]
    if production["group_name"] == experiment["group_name"]:
        raise RuntimeError("Experimental A2V group must not be production")
    if production["environment"]["LTX_WORKER_TASK_MODE"] != "a2v":
        raise RuntimeError("Unexpected production A2V task mode")
    if experiment["environment"]["LTX_WORKER_TASK_MODE"] != "a2v_experimental":
        raise RuntimeError("Unexpected experimental A2V task mode")
    if experiment["capacity"] != {"start_replicas": 1, "max_replicas": 1}:
        raise RuntimeError("Experimental A2V must have one-replica capacity")
    common = dict(
        api_key=_required("SALAD_API_KEY"),
        api_base_url=os.getenv("SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL),
        organization_name=_required("SALAD_ORGANIZATION"),
        project_name=_required("SALAD_PROJECT"),
    )
    if args.reuse_production and not args.expected_production_image:
        raise RuntimeError("--expected-production-image is required when reusing production")
    selected = production if args.reuse_production else experiment
    try:
        client = SaladClient(
            SaladDispatchConfig(
                **common, container_group_name=selected["group_name"]
            )
        )
        group = client.describe_container_group()
        assert_quiescent_group(
            group, expected_group_name=selected["group_name"],
            instances=client.list_container_group_instances(),
        )
        if args.reuse_production:
            image = group.get("container", {}).get("image")
            if image != args.expected_production_image:
                raise RuntimeError(
                    "Production group image mismatch; refusing to reuse or restore"
                )
            environment = group.get("container", {}).get("environment_variables", {})
            if environment.get("LTX_WORKER_TASK_MODE") != args.expected_task_mode:
                raise RuntimeError(
                    "Production task mode mismatch; refusing to reuse or restore"
                )
    except Exception as exc:
        print(
            "A2V isolation failed; selected group must be stopped and match its "
            f"expected image and task mode. Details: {exc}",
            file=sys.stderr,
        )
        return 1
    if args.reuse_production:
        print("A2V_ISOLATION_OK production=reuse-guarded stopped=true")
    else:
        print("A2V_ISOLATION_OK production=independent experiment=stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
