from __future__ import annotations

import os

from submit_phase8_smoke import main


if __name__ == "__main__":
    os.environ.setdefault(
        "SALAD_QUEUE_NAME",
        os.getenv("SALAD_LTX25_QUEUE_NAME", "ai-video-factory-ltx25-jobs"),
    )
    main()
