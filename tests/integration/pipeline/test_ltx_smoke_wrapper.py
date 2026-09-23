import os
import runpy
import sys
from pathlib import Path

WRAPPER = Path("scripts/smoke/submit_ltx25_smoke.py")


def _execute_wrapper() -> None:
    namespace = {
        "__name__": "__main__",
        "__file__": str(WRAPPER),
    }
    exec(compile(WRAPPER.read_text(encoding="utf-8"), str(WRAPPER), "exec"), namespace)


def test_ltx_smoke_overrides_legacy_generic_queue_and_bounds_pending(monkeypatch) -> None:
    monkeypatch.setenv("SALAD_QUEUE_NAME", "legacy-generic-queue")
    monkeypatch.setenv("SALAD_LTX25_QUEUE_NAME", "dedicated-ltx25-queue")
    monkeypatch.setattr(sys, "argv", [str(WRAPPER)])

    called = {}

    def fake_run_path(path: str, *, run_name: str) -> None:
        called["path"] = path
        called["run_name"] = run_name
        called["argv"] = list(sys.argv)

    monkeypatch.setattr(runpy, "run_path", fake_run_path)

    _execute_wrapper()

    assert os.environ["SALAD_QUEUE_NAME"] == "dedicated-ltx25-queue"
    assert called["path"].endswith("submit_phase8_smoke.py")
    assert called["run_name"] == "__main__"
    assert called["argv"][-2:] == ["--pending-timeout-seconds", "180"]
    assert sys.argv == [str(WRAPPER)]


def test_ltx_smoke_preserves_explicit_pending_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [str(WRAPPER), "--pending-timeout-seconds", "600"],
    )
    called = {}

    def fake_run_path(path: str, *, run_name: str) -> None:
        called["argv"] = list(sys.argv)

    monkeypatch.setattr(runpy, "run_path", fake_run_path)

    _execute_wrapper()

    assert called["argv"][-2:] == ["--pending-timeout-seconds", "600"]


