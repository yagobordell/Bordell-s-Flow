import sys
from pathlib import Path

import pytest

from ai_video_factory.workers.ideogram4.download_watchdog import run_with_progress_watchdog


def test_download_watchdog_allows_continuing_byte_progress(tmp_path: Path) -> None:
    progress_root = tmp_path / "snapshot"
    script = tmp_path / "progress.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "target = root / 'model.incomplete'\n"
        "for _ in range(5):\n"
        "    with target.open('ab') as handle:\n"
        "        handle.write(b'x' * 1024)\n"
        "        handle.flush()\n"
        "    time.sleep(0.04)\n",
        encoding="utf-8",
    )

    return_code = run_with_progress_watchdog(
        [sys.executable, str(script), str(progress_root)],
        progress_root=progress_root,
        stall_timeout_seconds=0.15,
        hard_timeout_seconds=2.0,
        poll_seconds=0.02,
    )

    assert return_code == 0
    assert (progress_root / "model.incomplete").stat().st_size == 5 * 1024


def test_download_watchdog_aborts_process_without_byte_progress(tmp_path: Path) -> None:
    progress_root = tmp_path / "snapshot"
    script = tmp_path / "stalled.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    with pytest.raises(TimeoutError, match="no byte progress"):
        run_with_progress_watchdog(
            [sys.executable, str(script)],
            progress_root=progress_root,
            stall_timeout_seconds=0.1,
            hard_timeout_seconds=1.0,
            poll_seconds=0.02,
        )


def test_download_watchdog_rejects_invalid_deadlines(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stall timeout"):
        run_with_progress_watchdog(
            [sys.executable, "-c", "pass"],
            progress_root=tmp_path,
            stall_timeout_seconds=2.0,
            hard_timeout_seconds=1.0,
            poll_seconds=0.1,
        )
