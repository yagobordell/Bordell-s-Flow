"""Run the experimental atomic 2-video/1-GPU task via Postgres/R2.

Keep this OUTSIDE production Phase 8. Both outputs and the mode report must
be committed and SHA-verified before this client claims success.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image

from ai_video_factory.config import settings
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.workers.ltx25.experimental_batch2 import LTX_I2V_BATCH2_TASK
from ai_video_factory.workers.ltx25.model import LTX_PRODUCTION_I2V_PROFILE


def parser() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Explicit experimental FP8 I2V B=2 comparison")
    p.add_argument("--image-a", type=Path, required=True)
    p.add_argument("--prompt-a", required=True)
    p.add_argument("--image-b", type=Path, required=True)
    p.add_argument("--prompt-b", required=True)
    p.add_argument("--seed-a", type=int, default=5243)
    p.add_argument("--seed-b", type=int, default=5244)
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--accept-full-hd-batch2-oom-risk", action="store_true")
    p.add_argument("--full-hd-stage2-mode", choices=("cpu", "disk"), default="cpu")
    p.add_argument("--vae-backend", choices=("eager", "triton_experimental"), default="eager")
    p.add_argument("--run-id", required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--reference-a", type=Path)
    p.add_argument("--reference-b", type=Path)
    p.add_argument("--allow-sequential-fallback", action="store_true")
    p.add_argument("--enable-stage-reuse", action="store_true")
    p.add_argument("--disable-stage-reuse", action="store_true")
    p.add_argument("--check-inputs-only", action="store_true")
    p.add_argument("--timeout-seconds", type=float, default=10800)
    return p.parse_args()


def validate_media(path: Path, *, width: int, height: int) -> None:
    if not path.is_file():
        raise ValueError(f"Missing input image: {path}")
    with Image.open(path) as image:
        if image.size != (width, height):
            raise ValueError(
                f"Batch2 requires native {width}x{height} PNG images; "
                f"{path}={image.size}"
            )
        if image.format != "PNG":
            raise ValueError(f"Expected lossless PNG input: {path}")


def validate_video(path: Path, *, width: int, height: int) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    decoder = shutil.which("ffmpeg")
    if not executable or not decoder:
        raise RuntimeError("ffprobe and ffmpeg are mandatory for artifact validation")
    result = subprocess.run(
        [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    probe = json.loads(result.stdout)
    video = [s for s in probe["streams"] if s["codec_type"] == "video"]
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    if len(video) != 1 or audio:
        raise ValueError("Each I2V artifact must contain exactly one VIDEO stream and no audio")
    if (int(video[0]["width"]), int(video[0]["height"])) != (width, height):
        raise ValueError("Batch2 output geometry does not match the B=1 reference")
    fps = video[0]["avg_frame_rate"]
    if fps != "24/1" or int(video[0].get("nb_frames") or 0) != 121:
        frames = video[0].get("nb_frames")
        raise ValueError(f"Batch2 output fps/frame count invalid: {fps}, {frames}")
    duration = float(probe["format"]["duration"])
    if abs(duration - 121 / 24) > 0.1:
        raise ValueError(f"Batch2 duration mismatch: {duration}")
    subprocess.run(
        [decoder, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        check=True, capture_output=True,
    )
    return {"width": width, "height": height, "fps": 24, "frames": 121, "duration": duration}


def required(name: str, value: str | None) -> str:
    if not value or not value.strip():
        raise RuntimeError(f"{name} must be configured before the paid batch2 trial")
    return value.strip()


def main() -> None:
    a = parser()
    if not a.run_id.replace("-", "").replace("_", "").isalnum() or len(a.run_id) > 70:
        raise ValueError("Run ID must have 1-70 safe characters")
    size = (a.width, a.height)
    if size not in {(1280, 720), (1920, 1080)}:
        raise ValueError("Batch2 smoke only supports native 1280x720 or 1920x1080")
    if a.vae_backend == "triton_experimental" and (
        size != (1920, 1080) or a.full_hd_stage2_mode != "cpu"
        or a.allow_sequential_fallback or a.enable_stage_reuse
    ):
        raise ValueError("Triton VAE requires 1080p CPU-stage2 true B2 without fallback")
    if size == (1920, 1080):
        if not a.accept_full_hd_batch2_oom_risk:
            raise ValueError(
                "1080p batch2 is unvalidated on 32-GiB RTX 5090; "
                "explicitly pass --accept-full-hd-batch2-oom-risk"
            )
        if a.allow_sequential_fallback or a.enable_stage_reuse:
            raise ValueError(
                "Full HD B2 trial forbids sequential fallback and "
                "unvalidated transformer stage reuse"
            )
    if a.enable_stage_reuse and a.disable_stage_reuse:
        raise ValueError("Stage reuse flags are mutually exclusive")
    if a.seed_a == a.seed_b or not 0 <= a.seed_a <= 2**31 - 1 or not 0 <= a.seed_b <= 2**31 - 1:
        raise ValueError("Use two distinct valid seeds")
    if not a.prompt_a.strip() or not a.prompt_b.strip():
        raise ValueError("Both prompts are mandatory")
    for item in (a.image_a, a.image_b):
        validate_media(item, width=a.width, height=a.height)
    if sha256_file(a.image_a) == sha256_file(a.image_b) and a.prompt_a == a.prompt_b:
        raise ValueError("Batch2 requires distinct images or prompts")
    references = []
    for path in (a.reference_a, a.reference_b):
        if path is None:
            references.append(None)
            continue
        if not path.is_file():
            raise ValueError(f"Existing reference video not found: {path}")
        references.append({"path": str(path), "sha256": sha256_file(path)})
    if a.vae_backend == "triton_experimental":
        if (
            a.reference_a is None or a.reference_b is None
            or a.reference_a.parent.resolve() != a.reference_b.parent.resolve()
            or a.reference_a.name != "batch2-a.mp4"
            or a.reference_b.name != "batch2-b.mp4"
        ):
            raise ValueError(
                "Triton trial requires BOTH matching prior eager B2 videos "
                "via --reference-a/b from the same baseline output directory"
            )
        baseline = a.reference_a.parent
        baseline_request = json.loads(
            (baseline / "job-request.json").read_text(encoding="utf-8")
        )["parameters"]
        baseline_inputs = json.loads(
            (baseline / "reference-provenance.json").read_text(encoding="utf-8")
        )["inputs"]["inputs"]
        if baseline_request.get("vae_backend", "eager") != "eager":
            raise ValueError("Triton baseline must use the original eager VAE")
        for key, path, prompt, seed in (
            ("keyframe_a", a.image_a, a.prompt_a, a.seed_a),
            ("keyframe_b", a.image_b, a.prompt_b, a.seed_b),
        ):
            reference = next(
                (item for item in baseline_inputs if item["name"] == key), None
            )
            old = baseline_request[key[-1]]
            if (
                reference is None or reference["sha256"] != sha256_file(path)
                or reference["prompt"] != prompt or reference["seed"] != seed
                or old["prompt"] != prompt or old["seed"] != seed
            ):
                raise ValueError("Triton trial input differs from eager reference")
    if a.check_inputs_only:
        print(
            f"Batch2 inputs valid: two native {a.width}x{a.height} PNGs, "
            "separate seeds and fixed 5s recipe"
        )
        return
    if a.output_dir.exists():
        raise ValueError("Run directory already exists; choose a new run ID")
    a.output_dir.mkdir(parents=True)
    plan = {
        "generation_profile": LTX_PRODUCTION_I2V_PROFILE,
        "width": a.width, "height": a.height, "fps": 24, "num_frames": 121,
    }
    inputs = (
        ("keyframe_a", a.image_a, a.prompt_a, a.seed_a),
        ("keyframe_b", a.image_b, a.prompt_b, a.seed_b),
    )
    spec = {
        "run_id": a.run_id,
        "inputs": [
            {"name": name, "sha256": sha256_file(path), "prompt": prompt, "seed": seed}
            for name, path, prompt, seed in inputs
        ],
        "allow_sequential_fallback": a.allow_sequential_fallback,
        "reuse_transformer_across_stages": a.enable_stage_reuse and not a.disable_stage_reuse,
        "accept_full_hd_batch2_oom_risk": a.accept_full_hd_batch2_oom_risk,
        "full_hd_stage2_mode": a.full_hd_stage2_mode,
        "vae_backend": a.vae_backend,
        "video_width": a.width,
        "video_height": a.height,
    }
    fingerprint = hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    job_id = f"ltx-batch2-{a.run_id}-{fingerprint}"
    object_inputs = []
    for name, path, _, _ in inputs:
        digest = sha256_file(path)
        object_inputs.append(ObjectInput(
            name=name,
            key=f"ltx25-i2v/batch2/{digest}.png",
            sha256=digest,
            content_type=mimetypes.guess_type(path.name)[0] or "image/png",
        ))
    req = InferenceJobRequest(
        job_id=job_id,
        task=LTX_I2V_BATCH2_TASK,
        inputs=object_inputs,
        output=ObjectOutput(key=f"jobs/{job_id}/batch2-a.mp4", content_type="video/mp4"),
        sidecar_outputs={
            "video_b": ObjectOutput(key=f"jobs/{job_id}/batch2-b.mp4", content_type="video/mp4"),
            "report": ObjectOutput(
                key=f"jobs/{job_id}/batch2-report.json", content_type="application/json"
            ),
        },
        max_attempts=1,  # No accidental paid reruns of an experimental OOM
        parameters={
            "a": {**plan, "prompt": a.prompt_a, "seed": a.seed_a},
            "b": {**plan, "prompt": a.prompt_b, "seed": a.seed_b},
            "allow_sequential_fallback": a.allow_sequential_fallback,
            "reuse_transformer_across_stages": a.enable_stage_reuse and not a.disable_stage_reuse,
            "accept_full_hd_batch2_oom_risk": a.accept_full_hd_batch2_oom_risk,
            "full_hd_stage2_mode": a.full_hd_stage2_mode,
        "vae_backend": a.vae_backend,
        },
    )
    (a.output_dir / "job-request.json").write_text(
        req.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (a.output_dir / "reference-provenance.json").write_text(
        json.dumps({"inputs": spec, "reference_videos": references}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    storage = create_r2_storage(
        endpoint_url=required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=required("R2_BUCKET", settings.r2_bucket),
        access_key_id=required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=required("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
    )
    executor = InferenceJobExecutor(
        queue=PostgresJobQueueClient(dsn=required("POSTGRES_DSN", settings.postgres_dsn)),
        storage=storage,
        poll_seconds=15,
        timeout_seconds=a.timeout_seconds,
        pending_timeout_seconds=1800,
    )
    for item, (_, path, _, _) in zip(object_inputs, inputs, strict=True):
        executor.ensure_input(
            path, key=item.key, sha256=item.sha256,
            content_type=item.content_type or "image/png",
            metadata={"purpose": "ltx25-experimental-batch2"},
        )
    response = executor.execute(req, metadata={"purpose": "ltx25-experimental-batch2"})
    if response.replayed:
        raise RuntimeError("Experiment reused an old result; metrics would be invalid")
    outputs = {
        "video_a": (req.output.key, a.output_dir / "batch2-a.mp4", response.output.sha256),
    }
    for name, key, filename in (
        ("video_b", req.sidecar_outputs["video_b"].key, "batch2-b.mp4"),
        ("report", req.sidecar_outputs["report"].key, "batch2-report.json"),
    ):
        obj = storage.stat(key)
        if obj is None:
            raise RuntimeError(f"Batch2 sidecar was not committed: {name}")
        outputs[name] = (key, a.output_dir / filename, obj.metadata.get("artifact-sha256"))
    for name, (key, path, digest) in outputs.items():
        storage.download(key, path)
        if not digest or sha256_file(path) != digest:
            raise RuntimeError(f"Batch2 R2 digest mismatch for {name}")
    report = json.loads((a.output_dir / "batch2-report.json").read_text(encoding="utf-8"))
    if report["mode"] not in (
        "batch2", "stage_shared_sequential_forward", "reference_sequential_oom_fallback"
    ):
        raise RuntimeError("Unknown execution mode; cannot claim batch2 succeeded")
    if not a.allow_sequential_fallback and report["mode"] != "batch2":
        raise RuntimeError("No-fallback experiment silently used sequential generation")
    report["technical_video_validation"] = {
        key: validate_video(
            a.output_dir / f"batch2-{suffix}.mp4", width=a.width, height=a.height,
        )
        for key, suffix in (("a", "a"), ("b", "b"))
    }
    report["replayed"] = response.replayed
    report["quality_status"] = "REFERENCE_VIDEO_SIDE_BY_SIDE_REVIEW_REQUIRED"
    # The durable report sidecar already contains the complete worker RAM
    # history. Export standalone JSON/CSV locally for plotting and sharing
    # without changing the atomic Postgres/R2 sidecar contract.
    if isinstance(report.get("memory_trace"), dict):
        ram = report["memory_trace"]
        (a.output_dir / "memory-trace.json").write_text(
            json.dumps(ram, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        samples = ram.get("samples", [])
        if samples:
            columns = list(dict.fromkeys(key for point in samples for key in point))
            with (a.output_dir / "memory-trace.csv").open(
                "w", newline="", encoding="utf-8",
            ) as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(samples)
    if a.vae_backend == "triton_experimental":
        from compare_ltx25_i2v_backend_trial import compare

        report["backend_comparison"] = compare(a.reference_a.parent, a.output_dir)
        report["quality_status"] = "HUMAN_SIDE_BY_SIDE_REVIEW_REQUIRED"
    (a.output_dir / "experiment-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Keep the terminal readable; the durable and local JSON files retain
    # every sample, while the CLI prints just the run's memory summary.
    display_report = dict(report)
    if isinstance(display_report.get("memory_trace"), dict):
        display_report["memory_trace"] = {
            key: value for key, value in display_report["memory_trace"].items()
            if key != "samples"
        }
    print(json.dumps(display_report, indent=2, sort_keys=True))
    print(f"Output directory: {a.output_dir}")


if __name__ == "__main__":
    main()
