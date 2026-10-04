"""Wait for the actual LTX worker readiness probe before submitting a paid I2V or A2V job.

This helper is intentionally read-only: it never starts, stops, updates or replaces
a Salad container group. The controlled PowerShell wrapper owns GPU cleanup.
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
from typing import Any

from dotenv import load_dotenv

from ai_video_factory.inference.salad import (
    DEFAULT_SALAD_API_BASE_URL,
    SaladClient,
    SaladDispatchConfig,
    SaladRequestError,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_TRANSIENT_API_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _required_salad_account_env(name: str) -> str:
    """Select the same explicit account as the Salad manager, never manifest defaults."""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"Missing {name} in the environment or .env")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value) is None:
        raise SystemExit(
            f"{name} must be a Salad organization/project slug "
            "(letters, digits, hyphens or underscores)"
        )
    return value


def _readiness_observation(
    group: Mapping[str, Any],
    instances: list[dict[str, object]],
    *,
    group_name: str,
    image_repository: str,
    expected_image: str = "",
    expected_task_mode: str = "",
) -> tuple[str | None, str]:
    """Accept only a current-version instance passing the configured /ready probe."""
    if group.get("name") != group_name:
        raise RuntimeError("Salad returned an unexpected LTX container group")
    probe = group.get("readiness_probe")
    http = probe.get("http") if isinstance(probe, dict) else None
    if not isinstance(http, dict) or http.get("path") != "/ready":
        raise RuntimeError("LTX group must have the HTTP /ready readiness probe")

    container = group.get("container")
    image = container.get("image") if isinstance(container, dict) else None
    if not isinstance(image, str) or _PINNED_IMAGE.fullmatch(image) is None:
        raise RuntimeError("LTX Salad image is not pinned to an immutable SHA256 digest")
    if image.split("@sha256:", maxsplit=1)[0] != image_repository:
        raise RuntimeError("LTX Salad image repository differs from the tracked manifest")
    if expected_image and image != expected_image:
        raise RuntimeError(
            "LTX Salad image does not match --expected-image; prepare the reviewed image first"
        )

    if expected_task_mode:
        environment = (
            container.get("environment_variables") if isinstance(container, dict) else None
        )
        if (
            not isinstance(environment, dict)
            or environment.get("LTX_WORKER_TASK_MODE") != expected_task_mode
        ):
            raise RuntimeError(
                "LTX group has wrong LTX_WORKER_TASK_MODE; refusing cross-service work"
            )

    version = group.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RuntimeError("Salad did not report a valid LTX container group version")
    state = group.get("current_state")
    status = state.get("status") if isinstance(state, dict) else None
    if status == "stopped":
        raise RuntimeError("LTX group stopped before its worker became ready")
    if (
        status not in {"running", "deploying", "pending"}
        or group.get("pending_change") is not False
    ):
        return None, f"group_status={status} pending={group.get('pending_change')}"

    if group.get("replicas") != 1:
        raise RuntimeError("Controlled LTX readiness requires exactly one configured replica")
    current_version_instances = [
        instance
        for instance in instances
        if instance.get("version") == version
        and instance.get("state") in {"allocating", "downloading", "creating", "running"}
    ]
    current_instances = [
        instance for instance in current_version_instances if instance.get("state") == "running"
    ]
    if len(current_instances) != 1:
        states = ",".join(
            f"{instance.get('id')}:{instance.get('state')}:"
            f"started={instance.get('started')}:ready={instance.get('ready')}"
            for instance in current_version_instances
        ) or "<none>"
        return None, (
            f"group_status={status} current_version={version} "
            f"running_current_instances={len(current_instances)} "
            f"current_version_instances={len(current_version_instances)} states=[{states}]"
        )

    instance = current_instances[0]
    # SaladCloud returns the instance identifier as "id", not "instance_id".
    instance_id = instance.get("id")
    if not isinstance(instance_id, str) or not instance_id:
        raise RuntimeError("Salad returned a running LTX instance without an instance ID")
    started = instance.get("started")
    ready = instance.get("ready")
    observation = (
        f"group_status={status} instance={instance_id} version={version} state=running "
        f"started={started} ready={ready}"
    )
    if started is True and ready is True:
        return instance_id, observation
    return None, observation


def wait_for_ready(
    client: SaladClient,
    *,
    group_name: str,
    image_repository: str,
    timeout_seconds: float,
    poll_seconds: float = 15.0,
    expected_image: str = "",
    expected_task_mode: str = "",
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = print,
) -> str:
    """Require two consecutive ready observations of the same current instance."""
    if not 0 < timeout_seconds <= 21600:
        raise ValueError("readiness timeout must be between 0 and 21600 seconds")
    if not 0 < poll_seconds <= 60:
        raise ValueError("readiness poll interval must be between 0 and 60 seconds")
    if expected_image and _PINNED_IMAGE.fullmatch(expected_image) is None:
        raise ValueError("expected image must use the immutable repo@sha256:digest form")

    started_at = clock()
    deadline = started_at + timeout_seconds
    last_report_at = float("-inf")
    last_observation = ""
    previous_ready_id: str | None = None
    consecutive_ready = 0
    transient_failures = 0

    while True:
        now = clock()
        if now >= deadline:
            raise TimeoutError(
                "LTX worker did not pass /ready within the bounded bootstrap budget "
                f"of {timeout_seconds:g}s; no inference job was submitted"
            )
        try:
            group = client.describe_container_group()
            instances = client.list_container_group_instances()
            ready_id, observation = _readiness_observation(
                group,
                instances,
                group_name=group_name,
                image_repository=image_repository,
                expected_image=expected_image,
                expected_task_mode=expected_task_mode,
            )
            transient_failures = 0
        except SaladRequestError as error:
            if error.status_code not in _TRANSIENT_API_STATUSES:
                raise
            transient_failures += 1
            if transient_failures >= 5:
                raise RuntimeError(
                    "Salad readiness API failed in five consecutive polls; "
                    "stopping the controlled smoke"
                ) from error
            ready_id = None
            observation = (
                f"Salad readiness API transient status={error.status_code} "
                f"consecutive_failures={transient_failures}"
            )

        if ready_id is not None and ready_id == previous_ready_id:
            consecutive_ready += 1
        elif ready_id is not None:
            consecutive_ready = 1
        else:
            consecutive_ready = 0
        previous_ready_id = ready_id

        now = clock()
        if observation != last_observation or now - last_report_at >= 60:
            report(
                f"LTX bootstrap elapsed_seconds={now - started_at:.1f} "
                f"{observation} consecutive_ready={consecutive_ready}/2"
            )
            last_observation = observation
            last_report_at = now
        if consecutive_ready >= 2 and ready_id is not None:
            report(
                f"LTX worker ready instance={ready_id} "
                f"bootstrap_wait_seconds={now - started_at:.1f}; "
                "the inference job may now be submitted"
            )
            return ready_id

        remaining = deadline - clock()
        if remaining <= 0:
            continue
        sleep(min(poll_seconds, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wait for one LTX Salad instance to pass the actual /ready probe."
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--expected-image", default="")
    parser.add_argument(
        "--reuse-production",
        action="store_true",
        help=(
            "Only with --service ltx25 and a pinned image: accept the explicitly "
            "prepared experimental A2V task mode instead of normal production mode."
        ),
    )
    parser.add_argument(
        "--check-config-only",
        action="store_true",
        help="Validate local account, service and image configuration without contacting Salad.",
    )
    parser.add_argument(
        "--service",
        choices=("ltx25", "ltx25_i2v", "ltx25_a2v_experimental"),
        default="ltx25",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file)
    if not env_file.is_absolute():
        env_file = _REPO_ROOT / env_file
    if not env_file.is_file():
        raise SystemExit(f"Salad environment file not found: {env_file}")
    load_dotenv(_REPO_ROOT / "config" / "app.env", override=False)
    load_dotenv(_REPO_ROOT / "config" / "salad.env", override=False)
    load_dotenv(env_file, override=False)

    manifest = json.loads(
        (_REPO_ROOT / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )
    ltx = manifest["services"][args.service]
    task_mode = ltx["environment"]["LTX_WORKER_TASK_MODE"]
    if task_mode not in {"i2v", "a2v", "a2v_experimental"}:
        raise SystemExit("Unsupported LTX worker task mode in the service manifest")
    expected_task_mode = task_mode
    if args.reuse_production:
        if args.service != "ltx25" or task_mode != "a2v" or not args.expected_image:
            raise SystemExit(
                "--reuse-production requires --service ltx25, the normal "
                "production a2v manifest mode and --expected-image"
            )
        experimental_mode = manifest["services"]["ltx25_a2v_experimental"][
            "environment"
        ]["LTX_WORKER_TASK_MODE"]
        if experimental_mode != "a2v_experimental":
            raise SystemExit(
                "Experimental task mode in the manifest changed; refusing shared group"
            )
        expected_task_mode = experimental_mode
    image = ltx["image"]
    image_repository = image.rsplit(":", maxsplit=1)[0]
    if args.expected_image:
        if _PINNED_IMAGE.fullmatch(args.expected_image) is None:
            raise SystemExit("Expected image must be pinned to repo@sha256:digest")
        if args.expected_image.split("@sha256:", maxsplit=1)[0] != image_repository:
            raise SystemExit("Expected image repository differs from the tracked I2V manifest")
    organization = _required_salad_account_env("SALAD_ORGANIZATION")
    project = _required_salad_account_env("SALAD_PROJECT")
    api_key = os.getenv("SALAD_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("SALAD_API_KEY is required to check worker readiness")
    if args.check_config_only:
        print(
            f"LTX readiness configuration OK service={args.service} "
            f"mode={expected_task_mode} account selected from environment"
        )
        return 0

    client = SaladClient(
        SaladDispatchConfig(
            api_key=api_key,
            api_base_url=os.getenv(
                "SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL
            ).strip(),
            organization_name=organization,
            project_name=project,
            container_group_name=ltx["group_name"],
        )
    )
    try:
        wait_for_ready(
            client,
            group_name=ltx["group_name"],
            image_repository=image_repository,
            expected_image=args.expected_image,
            expected_task_mode=expected_task_mode,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    except (RuntimeError, TimeoutError, ValueError) as error:
        print(f"LTX readiness failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
