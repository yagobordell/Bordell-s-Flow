from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from ai_video_factory.config import settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a local control-plane container.")
    parser.add_argument("--renderer", action="store_true")
    return parser.parse_args()


def _require_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"Required executable not found on PATH: {name}")
    return path


def main() -> None:
    args = parse_args()
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    smoke_path = data_dir / "tmp" / f"container-smoke-{os.getpid()}.txt"
    smoke_path.parent.mkdir(parents=True, exist_ok=True)
    smoke_path.write_text("ok\n", encoding="utf-8")
    smoke_path.unlink()

    print(f"python_control_plane=ok output_dir={settings.output_dir} temp_dir={settings.temp_dir}")

    if args.renderer:
        for executable in ("node", "npm", "npx", "ffmpeg", "ffprobe"):
            print(f"{executable}={_require_executable(executable)}")
        remotion_cli = Path("remotion/node_modules/.bin/remotion")
        if not remotion_cli.is_file():
            raise SystemExit(f"Remotion CLI is missing: {remotion_cli}")
        print(f"remotion_cli={remotion_cli.resolve()}")

    print("local_container_smoke=ok")


if __name__ == "__main__":
    main()
