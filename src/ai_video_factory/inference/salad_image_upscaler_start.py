"""Start an on-demand Salad image upscaler only when professional B2 needs x2."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from ai_video_factory.inference.salad import (
    DEFAULT_SALAD_API_BASE_URL,
    SaladClient,
    SaladDispatchConfig,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_PATH = _REPO_ROOT / "deploy" / "salad" / "image-upscaler-service.json"
_DEFAULT_TIMEOUT_SECONDS = 1800.0
_DEFAULT_POLL_SECONDS = 15.0


class ImageUpscalerStartupError(RuntimeError):
    """The required independent upscaler was unavailable before paid image work."""


def _account_settings() -> tuple[str | None, str | None]:
    # Settings() reads these env files but does not export account selectors.
    # Keep the same process-env-first behavior as the Salad deployment scripts.
    for path in (
        _REPO_ROOT / "config" / "app.env",
        _REPO_ROOT / "config" / "salad.env",
        _REPO_ROOT / ".env",
    ):
        load_dotenv(path, override=False)
    return os.getenv("SALAD_ORGANIZATION"), os.getenv("SALAD_PROJECT")


def _manifest_group_name(path: Path) -> str:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["schema_version"] != 1:
            raise ValueError("unsupported schema_version")
        group = manifest["service"]["group_name"]
        if not isinstance(group, str) or not group.strip():
            raise ValueError("missing group_name")
        return group
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ImageUpscalerStartupError(
            "Cannot load the independent Salad image-upscaler deployment manifest"
        ) from exc


def ensure_image_upscaler_ready(
    *,
    api_key: str,
    gateway_url: str,
    auto_start: bool = True,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
    manifest_path: Path = _MANIFEST_PATH,
    client: SaladClient | None = None,
    notify: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Ensure the single independent upscaler is ready, starting it if stopped.

    Return True only when this invocation actually submitted a start request.
    Never stop the group: another machine or professional run may share it.
    No GPT Image request may be submitted until this function succeeds.
    """
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("timeout_seconds and poll_seconds must be positive")
    hostname = urlsplit(gateway_url).hostname
    if urlsplit(gateway_url).scheme != "https" or not hostname:
        raise ImageUpscalerStartupError("IMAGE_UPSCALER_URL must be a valid HTTPS gateway URL")
    if not api_key or not api_key.strip():
        raise ImageUpscalerStartupError("SALAD_API_KEY is required for image-upscaler startup")

    group_name = _manifest_group_name(manifest_path)
    if client is None:
        organization, project = _account_settings()
        missing = [
            name for name, value in (
                ("SALAD_ORGANIZATION", organization),
                ("SALAD_PROJECT", project),
            ) if not value or not value.strip()
        ]
        if missing:
            raise ImageUpscalerStartupError(
                "Cannot automatically manage the Salad image upscaler: "
                + ", ".join(missing)
                + " are missing from the local environment"
            )
        client = SaladClient(SaladDispatchConfig(
            api_key=api_key,
            api_base_url=os.getenv("SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL),
            organization_name=organization,
            project_name=project,
            container_group_name=group_name,
        ))

    say = notify or (lambda message: print(message, flush=True))
    deadline = clock() + timeout_seconds
    started = False
    start_attempted = False
    previous_status: tuple[str, bool] | None = None
    status = "unknown"

    while True:
        group = client.describe_container_group()
        state = group.get("current_state")
        status = str(state.get("status") or "unknown") if isinstance(state, dict) else "unknown"
        pending = group.get("pending_change") is True
        networking = group.get("networking")
        dns = networking.get("dns") if isinstance(networking, dict) else None
        if isinstance(dns, str) and dns and dns.rstrip(".").lower() != hostname.lower():
            raise ImageUpscalerStartupError(
                "IMAGE_UPSCALER_URL does not match the configured Salad container group. "
                "Update config/salad.env with the URL printed by "
                "manage_image_upscaler_service.ps1 -Action Status."
            )

        if (status, pending) != previous_status:
            say(f"  Salad image upscaler: state={status}, pending={pending}")
            previous_status = (status, pending)

        if status == "running" and not pending:
            instances = client.list_container_group_instances()
            if any(
                instance.get("ready") is True and instance.get("state") == "running"
                for instance in instances
            ):
                say("  Salad image upscaler: instance ready.")
                return started

        if status == "stopped" and not pending and not start_attempted:
            if not auto_start:
                raise ImageUpscalerStartupError(
                    "Salad image upscaler is stopped and IMAGE_UPSCALER_AUTO_START=false. "
                    "Start it manually or enable automatic startup in config/salad.env."
                )
            say("  Salad image upscaler: starting one existing container-group replica...")
            # Also tolerates another runner that started it between GET and POST.
            start_attempted = True
            started = client.start_container_group_if_needed(warning_logger=say)

        if status in {"failed", "error"}:
            raise ImageUpscalerStartupError(
                f"Salad image upscaler entered {status}; inspect its instance logs."
            )
        remaining = deadline - clock()
        if remaining <= 0:
            raise ImageUpscalerStartupError(
                f"Salad image upscaler did not become ready within "
                f"{timeout_seconds:g}s (last state={status}). "
                "Inspect Salad instance logs before rerunning image generation."
            )
        sleep(min(poll_seconds, remaining))
