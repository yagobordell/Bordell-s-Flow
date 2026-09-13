from __future__ import annotations

import os
import runpy
from pathlib import Path

if __name__ == "__main__":
    os.environ["SALAD_QUEUE_NAME"] = os.getenv(
        "SALAD_LTX25_QUEUE_NAME",
        "ai-video-factory-ltx25-jobs",
    )
    runpy.run_path(
        str(Path(__file__).with_name("submit_phase8_smoke.py")),
        run_name="__main__",
    )
