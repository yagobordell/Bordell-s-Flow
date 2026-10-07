from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.workflows.b_video import (
    B2VideoError,
    reconcile_obsolete_b2_video_runs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cancel/supersede obsolete LTX jobs after a B2 source or production recipe "
            "change. Reusable immutable jobs are preserved and actively leased jobs are "
            "never force-cancelled."
        )
    )
    parser.add_argument(
        "output",
        type=Path,
        help="Exact saved pipeline output directory, e.g. .video_factory_work/enzo/test06",
    )
    return parser.parse_args()


def _read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"Required saved artifact is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read saved artifact: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Saved artifact is not a JSON object: {path}")
    return value


def main() -> None:
    args = parse_args()
    output = args.output.resolve()
    if output.is_symlink() or not output.is_dir():
        raise SystemExit(f"Saved pipeline output does not exist: {output}")
    if not settings.postgres_dsn:
        raise SystemExit("POSTGRES_DSN is required for LTX reconciliation")

    report = _read_json(output / "run_report.json")
    plan = _read_json(output / "visual_plan.json")
    images = _read_json(output / "images" / "manifest.json")
    run = report.get("run")
    if not isinstance(run, dict):
        raise SystemExit("Saved run report has no run object")
    audio = run.get("audio_generation")
    if (
        not isinstance(audio, dict)
        or audio.get("status") != "completed"
        or audio.get("stt_complete") is not True
    ):
        raise SystemExit("LTX reconciliation requires completed STT-aligned audio")
    if images.get("status") != "completed":
        raise SystemExit("LTX reconciliation requires completed regenerated images")
    if plan.get("visual_style") == "profesional" and (
        images.get("finishing_status") != "completed"
    ):
        raise SystemExit(
            "Professional LTX reconciliation requires completed finished images"
        )

    queue = PostgresJobQueueClient(dsn=settings.postgres_dsn)
    try:
        try:
            result = reconcile_obsolete_b2_video_runs(
                output,
                plan,
                audio,
                images,
                queue=queue,
            )
        except B2VideoError as exc:
            raise SystemExit(str(exc)) from None
    finally:
        queue.close()

    print(
        "LTX reconciliation complete: "
        f"new_digest={result['new_digest']} "
        f"superseded_runs={result['superseded_runs']} "
        f"cancelled_jobs={result['cancelled_jobs']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
