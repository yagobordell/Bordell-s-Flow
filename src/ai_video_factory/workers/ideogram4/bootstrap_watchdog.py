from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ai_video_factory.workers.download_watchdog import request_salad_reallocation

_TERMINAL_STAGE = "worker_ready"


def _read_status(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def watch_bootstrap(
    status_path: Path,
    *,
    stage_timeout_seconds: float,
    hard_timeout_seconds: float,
    poll_seconds: float,
    reallocate_on_stall: bool,
) -> None:
    if stage_timeout_seconds <= 0 or hard_timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("bootstrap watchdog timeouts and poll interval must be positive")
    if stage_timeout_seconds >= hard_timeout_seconds:
        raise ValueError("bootstrap stage timeout must be lower than hard timeout")

    started = time.time()
    print(
        "IDEOGRAM_BOOTSTRAP_WATCHDOG "
        f"status_path={status_path} stage_timeout_seconds={stage_timeout_seconds:g} "
        f"hard_timeout_seconds={hard_timeout_seconds:g}",
        flush=True,
    )

    while True:
        now = time.time()
        elapsed = now - started
        payload = _read_status(status_path)
        stage = "awaiting_status"
        stage_age = elapsed
        if payload is not None:
            stage = str(payload.get("stage") or "unknown")
            try:
                stage_started = float(payload.get("stage_started_epoch", started))
            except (TypeError, ValueError):
                stage_started = started
            stage_age = max(0.0, now - stage_started)

        print(
            "IDEOGRAM_BOOTSTRAP_PROGRESS "
            f"stage={stage} elapsed_seconds={elapsed:.1f} stage_seconds={stage_age:.1f}",
            flush=True,
        )

        if stage == _TERMINAL_STAGE:
            print("IDEOGRAM_BOOTSTRAP_WATCHDOG_COMPLETE", flush=True)
            return

        reason: str | None = None
        if elapsed >= hard_timeout_seconds:
            reason = (
                "Ideogram runtime bootstrap exceeded hard timeout of "
                f"{hard_timeout_seconds:g} seconds"
            )
        elif stage_age >= stage_timeout_seconds:
            reason = (
                f"Ideogram runtime bootstrap stage {stage!r} made no stage progress for "
                f"{stage_timeout_seconds:g} seconds"
            )

        if reason is not None:
            print(f"IDEOGRAM_BOOTSTRAP_TIMEOUT reason={reason}", flush=True)
            if reallocate_on_stall:
                requested = request_salad_reallocation(reason)
                print(
                    "SALAD_REALLOCATION_REQUEST "
                    f"source=ideogram_runtime_bootstrap requested={str(requested).lower()} "
                    f"reason={reason}",
                    flush=True,
                )
            raise TimeoutError(reason)

        time.sleep(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch Ideogram runtime bootstrap progress.")
    parser.add_argument("--status-path", type=Path, required=True)
    parser.add_argument("--stage-timeout-seconds", type=float, required=True)
    parser.add_argument("--hard-timeout-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--reallocate-on-stall", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    watch_bootstrap(
        args.status_path,
        stage_timeout_seconds=args.stage_timeout_seconds,
        hard_timeout_seconds=args.hard_timeout_seconds,
        poll_seconds=args.poll_seconds,
        reallocate_on_stall=args.reallocate_on_stall,
    )


if __name__ == "__main__":
    main()
