"""Wait for the actual LTX worker readiness probe before submitting a paid A2V job.

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


def _readiness_observation(
    group: Mapping[str, Any],
    instances: list[dict[str, object]],
    *,
    group_name: str,
    image_repository: str,
    expected_image: str = "",
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
        raise RuntimeError("Controlled LTX A2V readiness requires exactly one configured replica")
    current_instances = [
        instance
        for instance in instances
        if instance.get("state") == "running" and instance.get("version") == version
    ]
    if len(current_instances) != 1:
        return None, (
            f"group_status=running current_version={version} "
            f"running_current_instances={len(current_instances)}"
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
    args = parser.parse_args()

    env_file = Path(args.env_file)
    if not env_file.is_absolute():
        env_file = _REPO_ROOT / env_file
    if not env_file.is_file():
        raise SystemExit(f"Salad environment file not found: {env_file}")
    load_dotenv(env_file, override=False)

    manifest = json.loads(
        (_REPO_ROOT / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )
    stack = manifest["stack"]
    ltx = manifest["services"]["ltx25"]
    image = ltx["image"]
    image_repository = image.rsplit(":", maxsplit=1)[0]
    api_key = os.getenv("SALAD_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("SALAD_API_KEY is required to check worker readiness")

    client = SaladClient(
        SaladDispatchConfig(
            api_key=api_key,
            api_base_url=os.getenv(
                "SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL
            ).strip(),
            organization_name=stack["organization"],
            project_name=stack["project"],
            container_group_name=ltx["group_name"],
        )
    )
    try:
        wait_for_ready(
            client,
            group_name=ltx["group_name"],
            image_repository=image_repository,
            expected_image=args.expected_image,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    except (RuntimeError, TimeoutError, ValueError) as error:
        print(f"LTX readiness failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
