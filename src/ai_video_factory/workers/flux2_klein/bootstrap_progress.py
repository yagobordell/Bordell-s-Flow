from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class Flux2KleinBootstrapProgress:
    """Persist FLUX.2 Klein runtime bootstrap progress for an external watchdog."""

    def __init__(self, status_path: Path) -> None:
        self._status_path = status_path
        self._terminal = False

    def record(self, stage: str, **details: Any) -> None:
        if self._terminal and stage != "worker_ready":
            return
        now = time.time()
        payload: dict[str, Any] = {
            "stage": stage,
            "stage_started_epoch": now,
            "updated_epoch": now,
        }
        payload.update(details)
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self._status_path)
        if stage == "worker_ready":
            self._terminal = True
        rendered = " ".join(f"{key}={value}" for key, value in sorted(details.items()))
        suffix = f" {rendered}" if rendered else ""
        print(f"FLUX2_KLEIN_BOOTSTRAP_STAGE stage={stage}{suffix}", flush=True)
