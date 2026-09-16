from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class IdeogramBootstrapProgress:
    """Persist the current Ideogram runtime bootstrap stage for an external watchdog."""

    def __init__(self, status_path: Path) -> None:
        self._status_path = status_path
        self._stage_started_epoch = time.time()

    @property
    def status_path(self) -> Path:
        return self._status_path

    def record(self, stage: str, **details: Any) -> None:
        now = time.time()
        self._stage_started_epoch = now
        payload: dict[str, Any] = {
            "stage": stage,
            "stage_started_epoch": now,
            "updated_epoch": now,
        }
        payload.update(details)
        self._write(payload)
        rendered_details = " ".join(
            f"{key}={value}" for key, value in sorted(details.items())
        )
        suffix = f" {rendered_details}" if rendered_details else ""
        print(f"IDEOGRAM_BOOTSTRAP_STAGE stage={stage}{suffix}", flush=True)

    def _write(self, payload: dict[str, Any]) -> None:
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self._status_path)
