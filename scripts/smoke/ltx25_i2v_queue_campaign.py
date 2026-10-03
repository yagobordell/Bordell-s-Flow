"""Single-worker I2V comparison: 1 baseline job vs 2 concurrent SUBMISSIONS.

IMPORTANT: Existing LTX worker serializes GPU inference. This measures queue contention,
NOT two simultaneous diffusion forward passes on one GPU. Never advertise it as a
true same-GPU parallel inference benchmark. No production scheduler is modified.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ai_video_factory.inference.storage import sha256_file

SMOKE = Path(__file__).with_name("ltx25_i2v_smoke.py")
RUNNER = Path(__file__).resolve().parents[1] / "pipeline" / "run_phase8_videos.py"


def now() -> str:
    return datetime.now(UTC).isoformat()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def args_parser() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("prepare", "run"), required=True)
    parser.add_argument("--image-a", required=True, type=Path)
    parser.add_argument("--prompt-a", required=True)
    parser.add_argument("--image-b", required=True, type=Path)
    parser.add_argument("--prompt-b", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=5.0)
    parser.add_argument("--seed-base", type=int, default=4242)
    parser.add_argument("--timeout-seconds", type=int, default=10800)
    return parser.parse_args()


def trials(seed_base: int) -> list[dict[str, object]]:
    return [
        {"name": "baseline-a", "input": "a", "seed": seed_base},
        {"name": "queued-a", "input": "a", "seed": seed_base + 1000},
        {"name": "queued-b", "input": "b", "seed": seed_base + 1001},
    ]


def prepare(args: argparse.Namespace) -> None:
    if args.output_dir.exists():
        raise ValueError("Campaign already exists. Choose a fresh RunId; never reuse paid results.")
    if args.seed_base < 0 or args.seed_base + 1001 > 2147483646:
        raise ValueError("Invalid seed base.")
    if args.prompt_a.strip() == "" or args.prompt_b.strip() == "":
        raise ValueError("Both I2V motion prompts must be provided.")
    if (args.image_a.resolve() == args.image_b.resolve() and
            args.prompt_a.strip() == args.prompt_b.strip()):
        raise ValueError("Use distinct input images or prompts for the two concurrent jobs.")
    sources = {
        "a": (args.image_a.resolve(), args.prompt_a.strip()),
        "b": (args.image_b.resolve(), args.prompt_b.strip()),
    }
    plan = {
        "schema_version": 1,
        "purpose": "single-GPU serial-queue contention, NOT parallel GPU inference",
        "duration_seconds": args.duration_seconds,
        "sources": {
            name: {"sha256": sha256_file(image), "prompt": prompt}
            for name, (image, prompt) in sources.items()
        },
        "trials": trials(args.seed_base),
    }
    for item in plan["trials"]:
        name = str(item["name"])
        source = str(item["input"])
        image, prompt = sources[source]
        subprocess.run([
            sys.executable, str(SMOKE), "prepare",
            "--image", str(image),
            "--prompt", prompt,
            "--duration-seconds", str(args.duration_seconds),
            "--output-dir", str(args.output_dir / name),
        ], check=True)
    save(args.output_dir / "campaign-plan.json", plan)


def run_trial(args: argparse.Namespace, item: dict[str, object], log) -> subprocess.Popen:
    folder = args.output_dir / str(item["name"])
    inputs = folder / "inputs"
    output = folder / "output"
    argv = [
        sys.executable, str(RUNNER),
        "--keyframes", str(inputs / "storyboard_keyframes.json"),
        "--prompts", str(inputs / "video_prompts.json"),
        "--timings", str(inputs / "shot_timings.json"),
        "--output-dir", str(output),
        "--seed-base", str(item["seed"]),
        "--i2v-profile", "default",
        "--no-retry-terminal",
        "--timeout-seconds", str(args.timeout_seconds),
        "--poll-seconds", "15",
    ]
    return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)


def validate_trial(args: argparse.Namespace, item: dict[str, object]) -> dict[str, object]:
    folder = args.output_dir / str(item["name"])
    manifest_path = folder / "output" / "video_generation_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"{item['name']}: no job manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 1:
        raise RuntimeError(f"{item['name']}: expected exactly one job")
    job = jobs[0]
    if job.get("transport_status") != "succeeded" or job.get("submission_count") != 1:
        raise RuntimeError(f"{item['name']}: failed or replayed cached job")
    response = job.get("response") or {}
    if response.get("replayed") is True:
        raise RuntimeError(f"{item['name']}: R2 replay invalidates measured timing")
    video = folder / "output" / "video_clips" / "shot_001.mp4"
    subprocess.run([
        sys.executable, str(SMOKE), "validate", "--video", str(video),
        "--duration-seconds", str(args.duration_seconds),
        "--output-dir", str(folder),
    ], check=True)
    return {
        "status": "technically_valid_visual_review_pending",
        "application_job_id": job["application_job_id"],
        "sha256": sha256_file(video),
        "video_path": str(video.resolve()),
    }


def run(args: argparse.Namespace) -> None:
    plan = json.loads((args.output_dir / "campaign-plan.json").read_text(encoding="utf-8"))
    if plan["purpose"] != "single-GPU serial-queue contention, NOT parallel GPU inference":
        raise RuntimeError("Wrong campaign type")
    if args.duration_seconds != plan["duration_seconds"]:
        raise RuntimeError("Duration differs from preflight")
    for key, path, prompt in (
        ("a", args.image_a, args.prompt_a), ("b", args.image_b, args.prompt_b)
    ):
        previous = plan["sources"][key]
        if sha256_file(path) != previous["sha256"] or prompt.strip() != previous["prompt"]:
            raise RuntimeError("Inputs changed after paid-capacity preflight")
    records: dict[str, dict[str, object]] = {}
    summary = {
        "purpose": plan["purpose"],
        "worker_replicas": 1,
        "gpu_inference_parallelism": "NOT SUPPORTED by current worker",
        "baseline": None, "concurrent_submission": None, "trials": records,
    }
    report = args.output_dir / "campaign-results.json"
    save(report, summary)
    baseline, left, right = plan["trials"]
    try:
        for mode, items in (("baseline", [baseline]), ("concurrent_submission", [left, right])):
            running = []
            phase_start = time.monotonic()
            try:
                for item in items:
                    name = str(item["name"])
                    folder = args.output_dir / name
                    logfile = (folder / "runner.log").open("w", encoding="utf-8")
                    rec = {"status": "running", "submitted_utc": now()}
                    records[name] = rec
                    process = run_trial(args, item, logfile)
                    running.append((item, process, logfile, rec))
                # Both Phase-8 clients are alive concurrently; a single worker still
                # claims and runs only one GPU job at a time.
                for item, process, logfile, rec in running:
                    code = process.wait(timeout=args.timeout_seconds + 120)
                    logfile.close()
                    rec["finished_utc"] = now()
                    if code:
                        raise RuntimeError(f"{item['name']} runner failed ({code}); see runner.log")
                    rec.update(validate_trial(args, item))
                summary[mode] = {"client_wall_seconds": time.monotonic() - phase_start}
                save(report, summary)
            finally:
                for _, process, logfile, _ in running:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    if not logfile.closed:
                        logfile.close()
    finally:
        save(report, summary)
    print(f"Saved {report}; this campaign does NOT establish true parallel GPU inference.")


def main() -> None:
    args = args_parser()
    if args.mode == "prepare":
        prepare(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
