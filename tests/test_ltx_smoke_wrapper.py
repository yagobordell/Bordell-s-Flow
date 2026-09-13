import os
import runpy
from pathlib import Path

WRAPPER = Path("scripts/submit_ltx25_smoke.py")


def test_ltx_smoke_overrides_legacy_generic_queue(monkeypatch) -> None:
    monkeypatch.setenv("SALAD_QUEUE_NAME", "legacy-generic-queue")
    monkeypatch.setenv("SALAD_LTX25_QUEUE_NAME", "dedicated-ltx25-queue")

    called = {}

    def fake_run_path(path: str, *, run_name: str) -> None:
        called["path"] = path
        called["run_name"] = run_name

    monkeypatch.setattr(runpy, "run_path", fake_run_path)

    namespace = {
        "__name__": "__main__",
        "__file__": str(WRAPPER),
    }
    exec(compile(WRAPPER.read_text(encoding="utf-8"), str(WRAPPER), "exec"), namespace)

    assert os.environ["SALAD_QUEUE_NAME"] == "dedicated-ltx25-queue"
    assert called["path"].endswith("submit_phase8_smoke.py")
    assert called["run_name"] == "__main__"
