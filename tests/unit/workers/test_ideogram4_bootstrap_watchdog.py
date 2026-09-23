import json
import time
from pathlib import Path

import pytest

import ai_video_factory.workers.ideogram4.bootstrap_watchdog as bootstrap_watchdog


def _write_status(path: Path, *, stage: str, stage_started_epoch: float) -> None:
    path.write_text(
        json.dumps(
            {
                "stage": stage,
                "stage_started_epoch": stage_started_epoch,
                "updated_epoch": stage_started_epoch,
            }
        ),
        encoding="utf-8",
    )


def test_bootstrap_watchdog_accepts_worker_ready(tmp_path: Path) -> None:
    status = tmp_path / "bootstrap.json"
    _write_status(status, stage="worker_ready", stage_started_epoch=time.time())

    bootstrap_watchdog.watch_bootstrap(
        status,
        stage_timeout_seconds=1.0,
        hard_timeout_seconds=2.0,
        poll_seconds=0.01,
        reallocate_on_stall=False,
    )


def test_bootstrap_watchdog_reallocates_stalled_runtime_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = tmp_path / "bootstrap.json"
    _write_status(status, stage="from_pretrained", stage_started_epoch=time.time() - 10)
    reasons: list[str] = []

    monkeypatch.setattr(
        bootstrap_watchdog,
        "request_salad_reallocation",
        lambda reason: reasons.append(reason) or True,
    )

    with pytest.raises(TimeoutError, match="from_pretrained"):
        bootstrap_watchdog.watch_bootstrap(
            status,
            stage_timeout_seconds=1.0,
            hard_timeout_seconds=30.0,
            poll_seconds=0.01,
            reallocate_on_stall=True,
        )

    assert len(reasons) == 1
    assert "from_pretrained" in reasons[0]


def test_bootstrap_watchdog_rejects_unbounded_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lower than hard timeout"):
        bootstrap_watchdog.watch_bootstrap(
            tmp_path / "missing.json",
            stage_timeout_seconds=30.0,
            hard_timeout_seconds=30.0,
            poll_seconds=1.0,
            reallocate_on_stall=False,
        )
