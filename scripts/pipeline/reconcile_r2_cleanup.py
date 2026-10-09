"""Inspect or explicitly retry R2 cleanup for an already-published video.

Dry-run is the default. Never supply --apply before verifying the ownership
scope and confirming that all published clips are locally recoverable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_video_factory.compositor.static_preview import _verified_asset
from ai_video_factory.workflows.r2_cleanup import (
    _read_scoped_manifest,
    cleanup_published_video_jobs,
)


def inspect_published_cleanup(output: Path) -> dict[str, object]:
    report = output / "run_report.json"
    if report.is_symlink() or not report.is_file():
        raise ValueError("Missing or unsafe run report")
    document = json.loads(report.read_text(encoding="utf-8"))
    video = document.get("run", {}).get("video_generation", {})
    if not isinstance(video, dict) or video.get("status") != "completed":
        raise ValueError("Only published completed videos can be reconciled")
    _verified_asset(output, video.get("file"), video.get("sha256"))
    jobs, scope = _read_scoped_manifest(output, video)
    return {
        "status": "ready" if scope else "legacy_unscoped_no_cleanup",
        "scope": scope,
        "job_count": len(jobs),
        "job_ids": jobs,
        "video_sha256": video["sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Existing video run directory")
    parser.add_argument("--apply", action="store_true", help="Delete verified R2 keys")
    args = parser.parse_args()
    output = args.output.resolve()
    plan = inspect_published_cleanup(output)
    print(json.dumps(plan, indent=2))
    if not args.apply:
        print("DRY RUN ONLY: no Postgres, R2 or Salad mutations")
        return
    if plan["status"] != "ready":
        print("No scoped objects may be deleted")
        return
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    video = report["run"]["video_generation"]
    count = cleanup_published_video_jobs(output, video)
    print(f"Scoped R2 cleanup: {count} objects deleted")


if __name__ == "__main__":
    main()
