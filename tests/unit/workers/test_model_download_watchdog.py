import subprocess
import sys
from pathlib import Path

import pytest

from ai_video_factory.workers import download_watchdog


def test_shared_watchdog_rejects_sustained_slow_download_and_requests_reallocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_root = tmp_path / "model"
    script = tmp_path / "slow.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "target = root / 'weights.incomplete'\n"
        "for _ in range(100):\n"
        "    with target.open('ab') as handle:\n"
        "        handle.write(b'x' * 1024)\n"
        "        handle.flush()\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    reasons: list[str] = []
    monkeypatch.setattr(
        download_watchdog,
        "request_salad_reallocation",
        lambda reason: reasons.append(reason) or True,
    )

    with pytest.raises(TimeoutError, match="throughput"):
        download_watchdog.run_with_progress_watchdog(
            [sys.executable, str(script), str(progress_root)],
            progress_root=progress_root,
            stall_timeout_seconds=1.0,
            hard_timeout_seconds=3.0,
            poll_seconds=0.02,
            label="test-model",
            min_throughput_mib_per_second=1.0,
            throughput_grace_seconds=0.08,
            throughput_window_seconds=0.08,
            reallocate_on_slow=True,
        )

    assert len(reasons) == 1
    assert "throughput" in reasons[0]


def test_shared_watchdog_tiny_writes_do_not_mask_a_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_root = tmp_path / "model"
    script = tmp_path / "tiny-writes.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "target = root / 'weights.incomplete'\n"
        "for _ in range(200):\n"
        "    with target.open('ab') as handle:\n"
        "        handle.write(b'x' * 4096)\n"
        "        handle.flush()\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    reasons: list[str] = []
    monkeypatch.setattr(
        download_watchdog,
        "request_salad_reallocation",
        lambda reason: reasons.append(reason) or True,
    )

    with pytest.raises(TimeoutError, match="no meaningful byte progress"):
        download_watchdog.run_with_progress_watchdog(
            [sys.executable, str(script), str(progress_root)],
            progress_root=progress_root,
            stall_timeout_seconds=0.15,
            hard_timeout_seconds=3.0,
            poll_seconds=0.02,
            label="qwen-like-model",
            reallocate_on_slow=True,
            min_progress_reset_bytes=64 * 1024,
        )

    assert len(reasons) == 1
    assert "no meaningful byte progress" in reasons[0]


def test_shared_watchdog_meaningful_progress_resets_stall_timer(
    tmp_path: Path,
) -> None:
    progress_root = tmp_path / "model"
    script = tmp_path / "bursty.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "target = root / 'weights.incomplete'\n"
        "for _ in range(4):\n"
        "    with target.open('ab') as handle:\n"
        "        handle.write(b'x' * (128 * 1024))\n"
        "        handle.flush()\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )

    return_code = download_watchdog.run_with_progress_watchdog(
        [sys.executable, str(script), str(progress_root)],
        progress_root=progress_root,
        stall_timeout_seconds=0.2,
        hard_timeout_seconds=3.0,
        poll_seconds=0.02,
        label="bursty-model",
        min_progress_reset_bytes=64 * 1024,
    )

    assert return_code == 0


def test_shared_watchdog_does_not_reallocate_when_progress_is_fast_enough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_root = tmp_path / "model"
    script = tmp_path / "fast.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "target = root / 'weights.incomplete'\n"
        "for _ in range(32):\n"
        "    with target.open('ab') as handle:\n"
        "        handle.write(b'x' * (256 * 1024))\n"
        "        handle.flush()\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    reasons: list[str] = []
    monkeypatch.setattr(
        download_watchdog,
        "request_salad_reallocation",
        lambda reason: reasons.append(reason) or True,
    )

    return_code = download_watchdog.run_with_progress_watchdog(
        [sys.executable, str(script), str(progress_root)],
        progress_root=progress_root,
        stall_timeout_seconds=1.0,
        hard_timeout_seconds=3.0,
        poll_seconds=0.02,
        label="test-model",
        min_throughput_mib_per_second=1.0,
            throughput_grace_seconds=0.5,
            throughput_window_seconds=0.5,
        reallocate_on_slow=True,
    )

    assert return_code == 0
    assert reasons == []


def test_windows_process_termination_falls_back_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 42

        def __init__(self) -> None:
            self.terminated = False
            self.killed = False
            self.wait_calls = 0

        def poll(self) -> int | None:
            return -9 if self.killed else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: int) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            return -9

    process = FakeProcess()
    monkeypatch.setattr(download_watchdog.os, "name", "nt")

    download_watchdog._terminate_process_group(process)  # type: ignore[arg-type]

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2
