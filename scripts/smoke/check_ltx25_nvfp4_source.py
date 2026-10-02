"""Verify the optional official LTX-2.5 NVFP4 file at the pinned revision.

This is a credential-aware metadata-only preflight. No GPU is allocated and
no large model is downloaded. Any uncertainty fails closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv

MODEL_MANIFEST = Path("docker/workers/ltx25/model-manifest.json")
PINNED_MODEL_FILE = "diffusion_models/ltx-2.5-22b-distilled-transformer-nvfp4.safetensors"
_SHA = re.compile(r"[0-9a-f]{40}")


def require_official_nvfp4_available(manifest: dict[str, object], token: str) -> int:
    if manifest.get("repository") != "Lightricks/LTX-2.5":
        raise ValueError("NVFP4 must use the official Lightricks/LTX-2.5 source")
    revision = manifest.get("revision")
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise ValueError("NVFP4 requires a pinned 40-character model revision")
    if manifest.get("i2v_nvfp4_files") != [PINNED_MODEL_FILE]:
        raise ValueError("NVFP4 manifest must reference only the official distilled asset")
    url = (
        "https://huggingface.co/api/models/"
        "Lightricks/LTX-2.5/tree/"
        + quote(revision, safe="")
        + "/diffusion_models"
    )
    headers = {"User-Agent": "Bordell-Flow-NVFP4-Metadata-Preflight/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            listing = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Pinned HF revision listing inaccessible; do not start Salad") from exc
    if not isinstance(listing, list):
        raise RuntimeError("Unexpected official Hugging Face file-list payload")
    matches = [
        item for item in listing
        if isinstance(item, dict) and item.get("path") == PINNED_MODEL_FILE
    ]
    if len(matches) != 1 or int(matches[0].get("size") or 0) <= 0:
        raise RuntimeError(
            f"NVFP4 asset missing at pinned official revision {revision}; "
            "do not start Salad or automatically use HF main."
        )
    return int(matches[0]["size"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MODEL_MANIFEST)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    if args.env_file.is_file():
        load_dotenv(args.env_file, override=False)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    try:
        size = require_official_nvfp4_available(
            manifest, (os.environ.get("HF_TOKEN") or "").strip()
        )
    except (ValueError, RuntimeError) as exc:
        print(f"NVFP4_PREFLIGHT_FAILED {exc}", file=sys.stderr)
        return 1
    print(
        f"NVFP4_OFFICIAL_ASSET_AVAILABLE revision={manifest['revision']} "
        f"path={PINNED_MODEL_FILE} size_bytes={size}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
