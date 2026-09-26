from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ai_video_factory.workers.ltx25 as ltx25
from ai_video_factory.workers.ltx25 import LTX_A2V_GUIDED_GENERATION_PROFILE


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that the LTX smoke imports the selected worktree."
    )
    parser.add_argument("expected_module", type=Path)
    args = parser.parse_args()

    actual = Path(ltx25.__file__).resolve()
    expected = args.expected_module.resolve()
    if actual != expected:
        print(
            f"Wrong LTX Python source: {actual}; expected {expected}",
            file=sys.stderr,
        )
        return 1
    if not LTX_A2V_GUIDED_GENERATION_PROFILE:
        print("Guided A2V profile is missing.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
