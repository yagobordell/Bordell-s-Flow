"""Read-only I2V remote-deployment gate; never starts or patches a Salad group.

This MUST be paired with the offline exact-digest Docker ABI probe. The
remote group reporting that same immutable digest then connects both proofs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ai_video_factory.inference.salad import (
    DEFAULT_SALAD_API_BASE_URL,
    SaladClient,
    SaladDispatchConfig,
)

ROOT = Path(__file__).resolve().parents[2]
PINNED = re.compile(r"[^\s@]+@sha256:[a-fA-F0-9]{64}\Z")


def verify_remote_group(
    group: Mapping[str, Any],
    *,
    expected_group: str,
    expected_image: str,
    minimum_memory_mb: int = 61440,
) -> dict[str, Any]:
    """Fail closed on unknown remote fields or an already-running group."""
    if PINNED.fullmatch(expected_image) is None:
        raise ValueError("Expected immutable repo@sha256 image")
    if group.get("name") != expected_group:
        raise RuntimeError("Wrong Salad container group")
    state = group.get("current_state")
    if (
        not isinstance(state, dict)
        or state.get("status") != "stopped"
        or group.get("pending_change") is not False
    ):
        raise RuntimeError(
            "I2V group must be stably STOPPED before preparing a paid trial; "
            "inspect active operators and jobs without forcibly stopping it"
        )
    container = group.get("container")
    if not isinstance(container, dict) or container.get("image") != expected_image:
        raise RuntimeError(
            "Remote I2V image digest differs from the locally inspected Docker; "
            "prepare the STOPPED group with the validated immutable digest"
        )
    env = container.get("environment_variables")
    if (
        not isinstance(env, dict)
        or env.get("LTX_WORKER_TASK_MODE") != "i2v"
        or str(env.get("LTX_EXPERIMENTAL_I2V_BATCH2_ENABLED", "")).lower() != "true"
    ):
        raise RuntimeError("Remote group does not enable the isolated I2V batch2 task")
    resources = container.get("resources")
    memory = resources.get("memory") if isinstance(resources, dict) else None
    if isinstance(memory, bool) or not isinstance(memory, int):
        raise RuntimeError("Remote Salad memory reservation is absent or unverified")
    if memory < minimum_memory_mb:
        raise RuntimeError(
            f"Remote I2V memory={memory} MB is below {minimum_memory_mb} MB; "
            "local manifest alone does not certify remote capacity"
        )
    return {
        "group": expected_group,
        "status": "stopped",
        "image": expected_image,
        "memory_mb": memory,
        "hybrid_abi": "proven_by_separate_offline_digest_probe",
    }


def inspect_remote(client: SaladClient, **kwargs: Any) -> dict[str, Any]:
    """Only the existing read-only Salad describe endpoint is used."""
    return verify_remote_group(client.describe_container_group(), **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--minimum-memory-mb", type=int, default=61440)
    args = parser.parse_args()
    if PINNED.fullmatch(args.image) is None or args.minimum_memory_mb < 1:
        parser.error("Expected immutable image digest and positive memory budget")
    env_file = Path(args.env_file)
    if not env_file.is_absolute():
        env_file = ROOT / env_file
    if not env_file.is_file():
        parser.error(f"Missing environment file: {env_file}")
    load_dotenv(ROOT / "config" / "app.env", override=False)
    load_dotenv(ROOT / "config" / "salad.env", override=False)
    load_dotenv(env_file, override=False)
    manifest = json.loads(
        (ROOT / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )
    service = manifest["services"]["ltx25_i2v"]
    repository = service["image"].rsplit(":", 1)[0]
    if args.image.split("@sha256:", 1)[0] != repository:
        parser.error("Image repository does not match isolated I2V manifest")
    for name in ("SALAD_API_KEY", "SALAD_ORGANIZATION", "SALAD_PROJECT"):
        if not (os.getenv(name) or "").strip():
            parser.error(f"Missing {name} (read-only API inspection cannot proceed)")
    client = SaladClient(
        SaladDispatchConfig(
            api_key=os.environ["SALAD_API_KEY"].strip(),
            api_base_url=os.getenv(
                "SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL
            ).strip(),
            organization_name=os.environ["SALAD_ORGANIZATION"].strip(),
            project_name=os.environ["SALAD_PROJECT"].strip(),
            container_group_name=service["group_name"],
        )
    )
    result = inspect_remote(
        client,
        expected_group=service["group_name"],
        expected_image=args.image,
        minimum_memory_mb=args.minimum_memory_mb,
    )
    print("I2V_REMOTE_CONFIG_OK " + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
