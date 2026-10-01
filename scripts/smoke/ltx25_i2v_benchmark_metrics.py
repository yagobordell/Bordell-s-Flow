"""Persist per-run LTX 2.5 metrics from Salad log API or an exported baseline CSV.

The worker already logs complete CUDA-synchronized per-phase timings. This utility
downloads those exact records after each sequential smoke and refuses to silently
invent missing metrics. It never starts or modifies Salad capacity.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from ai_video_factory.workers.ltx25.model import (
    LTX_CFG_BATCH2_PROFILE,
    LTX_CFG_BATCH2_SILENT_PROFILE,
    LTX_CFG_BATCH2_TRITON_PROFILE,
    LTX_GENERATION_PROFILE,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    "default": LTX_GENERATION_PROFILE,
    "cfg-b2": LTX_CFG_BATCH2_PROFILE,
    "cfg-b2-silent": LTX_CFG_BATCH2_SILENT_PROFILE,
    "cfg-b2-triton": LTX_CFG_BATCH2_TRITON_PROFILE,
}
MARKERS = ("LTX25_I2V_", "INFERENCE_JOB_TASK_DONE")
_METRIC_MARKER = "LTX25_I2V_PHASE_METRICS "
_TASK_SECONDS = re.compile(r"\btask_seconds=(\d+(?:\.\d+)?)\b")
_ACCOUNT_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def _load_environment(env_file: Path) -> tuple[str, str, str, str]:
    if not env_file.is_absolute():
        env_file = REPO_ROOT / env_file
    if not env_file.is_file():
        raise ValueError(f"Missing environment file: {env_file}")
    for path in (
        REPO_ROOT / "config" / "app.env",
        REPO_ROOT / "config" / "salad.env",
        env_file,
    ):
        load_dotenv(path, override=False)
    organization = (os.getenv("SALAD_ORGANIZATION") or "").strip()
    project = (os.getenv("SALAD_PROJECT") or "").strip()
    key = (os.getenv("SALAD_API_KEY") or "").strip()
    if not _ACCOUNT_SLUG.fullmatch(organization):
        raise ValueError("Missing or invalid SALAD_ORGANIZATION")
    if not _ACCOUNT_SLUG.fullmatch(project):
        raise ValueError("Missing or invalid SALAD_PROJECT")
    if not key:
        raise ValueError("Missing SALAD_API_KEY")
    manifest = json.loads(
        (REPO_ROOT / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )
    group = manifest["services"]["ltx25_i2v"]["group_name"]
    return organization, project, key, group


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Log boundaries must include UTC timezone")
    return parsed.astimezone(timezone.utc)


def _log_query(
    *,
    organization: str,
    project: str,
    key: str,
    group: str,
    marker: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    url = f"https://api.salad.com/api/public/organizations/{organization}/log-entries"
    query = (
        'resource.type = "container" and '
        f'resource.labels.project_name = "{project}" and '
        f'resource.labels.container_group_name = "{group}" and '
        f'log contains "{marker}"'
    )
    body = json.dumps(
        {
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "page_size": 25,
            "sort_order": "desc",
            "query": query,
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Salad-Api-Key": key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ai-video-factory-ltx25-batch-metrics/1.0",
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise RuntimeError("Unexpected Salad log response: missing items list")
            if len(items) >= 25:
                raise RuntimeError(
                    "Salad log window hit page size; refusing to save incomplete phase metrics"
                )
            return items
        except HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise RuntimeError(f"Salad logs HTTP {error.code}") from error
        except (OSError, URLError) as error:
            if attempt == 2:
                raise RuntimeError("Salad log API unavailable") from error
        time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def _download_log_events(
    *,
    start: datetime,
    end: datetime,
    organization: str,
    project: str,
    key: str,
    group: str,
) -> list[dict[str, Any]]:
    events: dict[tuple[str, str], dict[str, Any]] = {}
    for marker in MARKERS:
        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(minutes=5), end)
            for event in _log_query(
                organization=organization,
                project=project,
                key=key,
                group=group,
                marker=marker,
                start=cursor,
                end=window_end,
            ):
                text = str(event.get("text_log") or "")
                timestamp = str(event.get("time") or "")
                if text:
                    events[(timestamp, text)] = {
                        "time": timestamp,
                        "text_log": text,
                    }
            cursor = window_end
    return sorted(events.values(), key=lambda event: event["time"])


def _extract_metrics(
    events: list[dict[str, Any]], *, profile: str, job_id: str = ""
) -> dict[str, Any]:
    expected = PROFILES[profile]
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for event in events:
        line = event["text_log"]
        if _METRIC_MARKER not in line:
            continue
        raw = line.split(_METRIC_MARKER, maxsplit=1)[1]
        try:
            metrics, _ = json.JSONDecoder().raw_decode(raw)
        except ValueError as error:
            raise ValueError("Corrupt LTX25_I2V_PHASE_METRICS JSON") from error
        if metrics.get("generation_profile") == expected:
            matches.append((event, metrics))
    if len(matches) != 1:
        raise ValueError(
            f"Expected one real phase metric for {profile}; found {len(matches)}. "
            "Check the exclusive group and exact job time window."
        )
    event, metrics = matches[0]
    task_events = [
        candidate
        for candidate in events
        if "INFERENCE_JOB_TASK_DONE " in candidate["text_log"]
        and (not job_id or f"job_id={job_id} " in candidate["text_log"])
    ]
    if job_id and len(task_events) != 1:
        raise ValueError(
            f"Expected exactly one TASK_DONE for job {job_id}; found {len(task_events)}"
        )
    task_seconds = None
    if len(task_events) == 1:
        match = _TASK_SECONDS.search(task_events[0]["text_log"])
        if match:
            task_seconds = float(match.group(1))
    return {
        "schema_version": 1,
        "profile": profile,
        "generation_profile": expected,
        "metric_log_time": event["time"],
        "job_id": job_id or None,
        "task_seconds": task_seconds,
        "metrics": metrics,
        "events": events,
    }


def _import_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "Text Log" not in reader.fieldnames:
            raise ValueError("Not a Salad CSV export: missing 'Text Log' column")
        return [
            {"time": row.get("Time", ""), "text_log": row["Text Log"]}
            for row in reader
            if row.get("Text Log")
        ]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _summarize(batch_dir: Path) -> None:
    rows: list[dict[str, Any]] = []
    for result_path in sorted(batch_dir.glob("*/run-result.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        metrics_path = result_path.parent / "phase-metrics.json"
        row = {
            "profile": result["profile"],
            "status": result["status"],
            "run_id": result.get("run_id"),
            "job_id": result.get("job_id"),
            "gpu_run_start_utc": result.get("gpu_run_start_utc"),
            "gpu_run_end_utc": result.get("gpu_run_end_utc"),
            "client_wall_seconds": result.get("client_wall_seconds"),
            "video_sha256": result.get("video_sha256"),
            "metrics_source": None,
        }
        if metrics_path.is_file():
            saved = json.loads(metrics_path.read_text(encoding="utf-8"))
            row["metrics_source"] = saved.get("source")
            row["task_seconds"] = saved.get("task_seconds")
            row.update(saved.get("metrics") or {})
        rows.append(row)
    if not rows:
        raise ValueError("No run-result.json files found in benchmark batch")
    _write_json(batch_dir / "summary.json", rows)
    preferred = [
        "profile", "status", "run_id", "job_id", "metrics_source",
        "client_wall_seconds", "total_generation_seconds", "task_seconds",
        "pipeline_acquire_seconds", "prompt_encode_seconds",
        "image_conditioning_stage1_seconds", "stage1_total_seconds",
        "spatial_upsample_seconds", "image_conditioning_stage2_seconds",
        "stage2_total_seconds", "audio_decode_seconds", "video_decode_seconds",
        "encode_including_decode_seconds", "encode_excluding_video_decode_seconds",
        "cuda_peak_allocated_mb", "cuda_peak_reserved_mb",
        "stage1_max_batch_size", "stage1_expected_forward_calls",
        "stage2_expected_forward_calls", "diffvae_backend_effective",
        "pipeline_reused", "gpu_run_start_utc", "gpu_run_end_utc", "video_sha256",
    ]
    all_keys = set().union(*(set(row) for row in rows))
    columns = [key for key in preferred if key in all_keys]
    columns.extend(sorted(all_keys - set(columns)))
    with (batch_dir / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Benchmark metrics saved: {batch_dir / 'summary.csv'}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("collect", "from-csv"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--profile", choices=tuple(PROFILES), required=True)
        cmd.add_argument("--output", type=Path, required=True)
        cmd.add_argument("--job-id", default="")
        if action == "collect":
            cmd.add_argument("--env-file", type=Path, default=Path(".env"))
            cmd.add_argument("--start-utc", required=True)
            cmd.add_argument("--end-utc", required=True)
            cmd.add_argument("--settle-seconds", type=int, default=60)
        else:
            cmd.add_argument("--csv", type=Path, required=True)
    cmd = sub.add_parser("summarize")
    cmd.add_argument("--batch-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "summarize":
            _summarize(args.batch_dir)
            return 0
        if args.action == "from-csv":
            events = _import_csv(args.csv)
            saved = _extract_metrics(events, profile=args.profile, job_id=args.job_id)
            saved["source"] = "salad-csv"
        else:
            start = _utc(args.start_utc)
            end = _utc(args.end_utc)
            if end <= start:
                raise ValueError("End must be later than start")
            organization, project, key, group = _load_environment(args.env_file)
            deadline = time.monotonic() + args.settle_seconds
            while True:
                events = _download_log_events(
                    start=start, end=end, organization=organization,
                    project=project, key=key, group=group,
                )
                try:
                    saved = _extract_metrics(
                        events, profile=args.profile, job_id=args.job_id
                    )
                    break
                except ValueError as error:
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            f"Required phase metrics did not arrive: {error}"
                        ) from error
                    print(f"Waiting for Salad phase metrics: {error}", flush=True)
                    time.sleep(min(10.0, max(0.0, deadline - time.monotonic())))
            saved["source"] = "salad-api"
            saved["query_start_utc"] = start.isoformat()
            saved["query_end_utc"] = end.isoformat()
        _write_json(args.output, saved)
        print(
            f"LTX benchmark metrics saved profile={args.profile} "
            f"total_seconds={saved['metrics']['total_generation_seconds']:.3f} "
            f"output={args.output}",
            flush=True,
        )
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"LTX benchmark metrics FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
