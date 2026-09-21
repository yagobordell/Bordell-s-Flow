import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def _load_script() -> ModuleType:
    path = Path("scripts/run_phase8_videos.py")
    spec = importlib.util.spec_from_file_location("run_phase8_videos", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    scripts_dir = str(path.parent.resolve())
    sys.path.insert(0, scripts_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts_dir)
    return module


def test_ltx25_queue_name_has_model_specific_default(monkeypatch) -> None:
    monkeypatch.delenv("SALAD_LTX25_QUEUE_NAME", raising=False)
    monkeypatch.delenv("SALAD_QUEUE_NAME", raising=False)
    module = _load_script()

    assert module._default_queue_name() == "ai-video-factory-ltx25-jobs-v2"


def test_ltx25_queue_name_prefers_dedicated_setting(monkeypatch) -> None:
    monkeypatch.setenv("SALAD_QUEUE_NAME", "legacy-queue")
    monkeypatch.setenv("SALAD_LTX25_QUEUE_NAME", "dedicated-ltx")
    module = _load_script()

    assert module._default_queue_name() == "dedicated-ltx"


def test_ltx25_queue_name_ignores_legacy_generic_queue(monkeypatch) -> None:
    monkeypatch.delenv("SALAD_LTX25_QUEUE_NAME", raising=False)
    monkeypatch.setenv("SALAD_QUEUE_NAME", "legacy-queue")
    module = _load_script()

    assert module._default_queue_name() == "ai-video-factory-ltx25-jobs-v2"
    assert os.environ["SALAD_QUEUE_NAME"] == "legacy-queue"
