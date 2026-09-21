from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    os.environ["SALAD_QUEUE_NAME"] = os.getenv(
        "SALAD_LTX25_QUEUE_NAME",
        "ai-video-factory-ltx25-jobs-v2",
    )
    original_argv = sys.argv
    try:
        if "--pending-timeout-seconds" not in sys.argv:
            sys.argv = [*sys.argv, "--pending-timeout-seconds", "180"]
        runpy.run_path(
            str(Path(__file__).with_name("submit_phase8_smoke.py")),
            run_name="__main__",
        )
    finally:
        sys.argv = original_argv
