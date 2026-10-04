"""Safely submit an isolated 1080p true-GPU-B2 A2V trial via Postgres/R2.

No Salad start here: the operator MUST explicitly prepare and start the
dedicated experimental group after offline Docker and input checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image
from submit_ltx25_a2v_smoke import _validate_decodable_video, _validate_reference_voice_tail

from ai_video_factory.config import settings
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_RUN_STATIC_FAST_PROFILE,
    _validate_audio_duration,
    probe_audio,
)
from ai_video_factory.workers.ltx25.experimental_a2v_batch2 import (
    LTX_A2V_BATCH2_TASK,
    A2VBatch2Parameters,
)
from ai_video_factory.workers.ltx25.reference_recipe import (
    reference_num_frames_for_samples,
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    for suffix in ("a", "b"):
        p.add_argument("--image-" + suffix, type=Path, required=True)
        p.add_argument("--audio-" + suffix, type=Path, required=True)
        p.add_argument("--prompt-" + suffix, required=True)
        p.add_argument("--seed-" + suffix, type=int, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--reference-a", type=Path)
    p.add_argument("--reference-b", type=Path)
    p.add_argument("--accept-full-hd-oom-risk", action="store_true")
    p.add_argument(
        "--vae-backend", choices=("eager", "triton_experimental"), default="eager",
    )
    p.add_argument(
        "--guidance-forward-batch-size", type=int, choices=(2, 4), default=2,
    )
    p.add_argument("--max-stage1-major-page-faults", type=int, default=0)
    p.add_argument("--progressive-block-group-size", type=int,
                   choices=(1, 2, 4, 8), default=1)
    p.add_argument("--min-checkpoint-direct-read-mibps", type=float, default=0.0)
    p.add_argument("--max-stage1-build-seconds", type=float, default=0.0)
    p.add_argument("--performance-target-seconds", type=float, default=0.0)
    p.add_argument("--check-inputs-only", action="store_true")
    p.add_argument("--timeout-seconds", type=float, default=10800)
    return p


def _validate_picture(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"Missing avatar: {path}")
    with Image.open(path) as image:
        if image.format != "PNG" or image.width < 128 or image.height < 128:
            raise ValueError("A2V trial requires valid lossless PNG avatar images")


def validate_inputs(a: argparse.Namespace) -> dict[str, Any]:
    if not a.run_id or len(a.run_id) > 65 or not all(
        ch.isascii() and (ch.isalnum() or ch in "-_") for ch in a.run_id
    ):
        raise ValueError("Run ID must contain 1-65 ASCII letters, digits, - or _")
    if not 0 <= a.max_stage1_major_page_faults <= 1000000:
        raise ValueError("Invalid experimental major-page-fault limit")
    if not 0 <= a.performance_target_seconds <= 10800:
        raise ValueError("Invalid experimental wall-time target")
    if not 0 <= a.min_checkpoint_direct_read_mibps <= 10000:
        raise ValueError("Invalid experimental direct checkpoint I/O minimum")
    if not 0 <= a.max_stage1_build_seconds <= 3600:
        raise ValueError("Invalid experimental stage1 model build cap")
    if a.seed_a == a.seed_b:
        raise ValueError("Separate A2V B2 seeds are mandatory")
    if bool(a.reference_a) != bool(a.reference_b):
        raise ValueError("Supply both reference videos or neither")
    if a.vae_backend == "triton_experimental" and a.reference_a is None:
        raise ValueError("Triton requires both matching approved Eager B2 reference MP4s")
    if a.guidance_forward_batch_size == 4 and a.reference_a is None:
        raise ValueError(
            "Fused B4 guidance requires both matching approved Eager/split-B2 reference MP4s"
        )
    pictures = (a.image_a, a.image_b)
    audios = (a.audio_a, a.audio_b)
    for image in pictures:
        _validate_picture(image)
    frames = []
    audio_facts = []
    for audio in audios:
        probe = probe_audio(audio)
        _validate_audio_duration(probe, fps=24)
        count = reference_num_frames_for_samples(
            probe.sample_count, sample_rate=probe.sample_rate, fps=24,
        )
        frames.append(count)
        audio_facts.append({
            "file": str(audio), "sha256": sha256_file(audio),
            "sample_count": probe.sample_count,
            "sample_rate": probe.sample_rate,
            "audio_seconds": probe.duration_seconds,
            "num_frames": count,
        })
    if frames[0] != frames[1]:
        raise ValueError(
            "Cannot safely batch audios with different 8k+1 frames: "
            f"{frames[0]} vs {frames[1]}"
        )
    plan = {
        "generation_profile": LTX_A2V_RUN_STATIC_FAST_PROFILE,
        "width": 1920, "height": 1080, "fps": 24,
    }
    params = A2VBatch2Parameters.model_validate({
        "a": {**plan, "prompt": a.prompt_a, "seed": a.seed_a},
        "b": {**plan, "prompt": a.prompt_b, "seed": a.seed_b},
        "stage2_mode": "cpu",
        "vae_backend": a.vae_backend,
        "guidance_forward_batch_size": a.guidance_forward_batch_size,
        "max_stage1_major_page_faults": a.max_stage1_major_page_faults,
        "progressive_block_group_size": a.progressive_block_group_size,
        "min_checkpoint_direct_read_mibps": a.min_checkpoint_direct_read_mibps,
        "max_stage1_build_seconds": a.max_stage1_build_seconds,
        "accept_full_hd_oom_risk": a.accept_full_hd_oom_risk,
    })
    provenance = {
        "images": [sha256_file(x) for x in pictures],
        "audio": audio_facts,
        "prompts": [params.a.prompt, params.b.prompt],
        "seeds": [params.a.seed, params.b.seed],
        "num_frames": frames[0],
        "profile": LTX_A2V_RUN_STATIC_FAST_PROFILE,
        "geometry": [1920, 1080, 24],
        "vae_backend": a.vae_backend,
        "guidance_forward_batch_size": a.guidance_forward_batch_size,
        "max_stage1_major_page_faults": a.max_stage1_major_page_faults,
        "progressive_block_group_size": a.progressive_block_group_size,
        "min_checkpoint_direct_read_mibps": a.min_checkpoint_direct_read_mibps,
        "max_stage1_build_seconds": a.max_stage1_build_seconds,
        "performance_target_seconds": a.performance_target_seconds,
    }
    for path in (a.reference_a, a.reference_b):
        if path is not None and (not path.is_file() or path.stat().st_size == 0):
            raise ValueError(f"Missing B1 reference video: {path}")
    if a.vae_backend == "triton_experimental" or a.guidance_forward_batch_size == 4:
        provenance["eager_baseline"] = _require_matching_eager_baseline(
            a.reference_a, a.reference_b, provenance,
        )
    return {"params": params, "provenance": provenance}


def _require_matching_eager_baseline(
    video_a: Path, video_b: Path, candidate: dict[str, Any],
) -> dict[str, Any]:
    """Reject wrong-input videos BEFORE any billed GPU starts.

    The two baseline MP4s must belong to ONE immutable eager B2 run with
    original request, provenance and verified technical/audio output report.
    """
    directory = video_a.resolve().parent
    if video_b.resolve().parent != directory:
        raise ValueError("Both Eager reference MP4s must come from the same run")
    if (video_a.name, video_b.name) != ("batch2-a.mp4", "batch2-b.mp4"):
        raise ValueError("Use the original Eager batch2-a/b.mp4 from one run")
    artifacts = (
        directory / "job-request.json",
        directory / "reference-provenance.json",
        directory / "experiment-summary.json",
    )
    if any(not p.is_file() for p in artifacts):
        raise ValueError("Eager baseline must include its original three JSON manifests")
    request, source, report = (
        json.loads(p.read_text(encoding="utf-8")) for p in artifacts
    )
    original_params = request["parameters"]
    required = A2VBatch2Parameters.model_validate(original_params)
    if required.vae_backend != "eager" or required.guidance_forward_batch_size != 2:
        raise ValueError("Optimization reference must be original Eager/split-B2 pair")
    if (
        request.get("task") != LTX_A2V_BATCH2_TASK
        or report.get("mode") != "batch2"
        or report.get("actual_gpu_batch_size") != 2
        or report.get("video_vae_backend") != "eager"
        or report.get("audio_tail_validation") != "PASSED_BOTH_OUTPUTS"
        or sorted(report.get("technical_validation", {})) != ["a", "b"]
    ):
        raise ValueError("Reference is not a fully verified Eager batch2 pair")
    for key in ("images", "prompts", "seeds", "num_frames", "profile", "geometry"):
        if source.get(key) != candidate.get(key):
            raise ValueError(f"Eager B2 baseline mismatch: {key}")
    if [
        (x["sha256"], x["sample_rate"], x["sample_count"], x["num_frames"])
        for x in source["audio"]
    ] != [
        (x["sha256"], x["sample_rate"], x["sample_count"], x["num_frames"])
        for x in candidate["audio"]
    ]:
        raise ValueError("Eager B2 audio source does not match the new pair")
    for label, video in (("a", video_a), ("b", video_b)):
        if not video.is_file() or not video.stat().st_size:
            raise ValueError(f"Missing full Eager baseline video {label}")
        entry = report["technical_validation"][label]
        if (
            entry.get("width"), entry.get("height"), entry.get("fps"),
            entry.get("num_frames"),
        ) != (1920, 1080, 24, candidate["num_frames"]):
            raise ValueError(f"Eager baseline MP4 technical mismatch {label}")
    return {
        "baseline_job_id": request["job_id"],
        "baseline_video_sha256": [sha256_file(video_a), sha256_file(video_b)],
        "baseline_report_sha256": sha256_file(artifacts[2]),
    }


def _required(name: str, value: str | None) -> str:
    if not value or not value.strip():
        raise RuntimeError(f"{name} must be configured for paid Postgres/R2 trial")
    return value.strip()


def _technical_video(path: Path, *, fps: int, num_frames: int) -> dict[str, Any]:
    binary = shutil.which("ffprobe")
    if not binary:
        raise RuntimeError("ffprobe required to verify A2V video and audio stream")
    result = subprocess.run(
        [binary, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    metadata = json.loads(result.stdout)
    videos = [s for s in metadata["streams"] if s["codec_type"] == "video"]
    audio = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audio) != 1:
        raise RuntimeError("Each B2 video must have exactly one video and one audio stream")
    v = videos[0]
    if (int(v["width"]), int(v["height"])) != (1920, 1080):
        raise RuntimeError("Wrong 1080p output geometry")
    if v["avg_frame_rate"] != "24/1" or int(v.get("nb_frames") or 0) != num_frames:
        raise RuntimeError("A2V output video fps/frames differ from audio grid")
    _validate_decodable_video(path)
    return {
        "width": 1920, "height": 1080, "fps": fps,
        "num_frames": num_frames,
        "duration_seconds": float(metadata["format"]["duration"]),
        "has_audio_stream": True,
    }


def main() -> None:
    a = parser().parse_args()
    inputs = validate_inputs(a)
    params, provenance = inputs["params"], inputs["provenance"]
    if a.check_inputs_only:
        print("A2V_B2_INPUTS_OK " + json.dumps(provenance, sort_keys=True))
        return
    if a.output_dir.exists():
        raise ValueError("Output directory exists; choose a new run ID")
    a.output_dir.mkdir(parents=True)
    fingerprint = hashlib.sha256(
        json.dumps({"run_id": a.run_id, **provenance}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    job_id = "ltx-a2v-b2-" + a.run_id + "-" + fingerprint
    specs = (
        ("image_a", a.image_a, "image/png"),
        ("audio_a", a.audio_a, mimetypes.guess_type(a.audio_a.name)[0] or "audio/wav"),
        ("image_b", a.image_b, "image/png"),
        ("audio_b", a.audio_b, mimetypes.guess_type(a.audio_b.name)[0] or "audio/wav"),
    )
    object_inputs = [
        ObjectInput(
            name=name, key=f"ltx25-a2v/batch2/{sha256_file(path)}-{name}{path.suffix}",
            sha256=sha256_file(path), content_type=content_type,
        )
        for name, path, content_type in specs
    ]
    req = InferenceJobRequest(
        job_id=job_id, task=LTX_A2V_BATCH2_TASK,
        inputs=object_inputs,
        output=ObjectOutput(
            key=f"jobs/{job_id}/batch2-a.mp4", content_type="video/mp4",
        ),
        sidecar_outputs={
            "video_b": ObjectOutput(
                key=f"jobs/{job_id}/batch2-b.mp4", content_type="video/mp4",
            ),
            "report": ObjectOutput(
                key=f"jobs/{job_id}/batch2-report.json", content_type="application/json",
            ),
        },
        max_attempts=1,
        parameters=params.model_dump(),
    )
    (a.output_dir / "job-request.json").write_text(req.model_dump_json(indent=2) + "\n")
    (a.output_dir / "reference-provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
    )
    storage = create_r2_storage(
        endpoint_url=_required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
    )
    executor = InferenceJobExecutor(
        queue=PostgresJobQueueClient(
            dsn=_required("POSTGRES_DSN", settings.postgres_dsn),
        ),
        storage=storage, poll_seconds=15,
        timeout_seconds=a.timeout_seconds, pending_timeout_seconds=1800,
    )
    for spec, item in zip(specs, object_inputs, strict=True):
        _, path, _ = spec
        executor.ensure_input(
            path, key=item.key, sha256=item.sha256,
            content_type=item.content_type,
            metadata={"purpose": "ltx25-a2v-true-b2-experiment"},
        )
    response = executor.execute(
        req, metadata={"purpose": "ltx25-a2v-true-b2-experiment"},
    )
    if response.replayed:
        raise RuntimeError("Replayed prior A2V trial cannot establish valid timings")
    files = {
        "video_a": (req.output.key, a.output_dir / "batch2-a.mp4",
                    response.output.sha256),
    }
    for name, ext in (("video_b", "mp4"), ("report", "json")):
        entry = req.sidecar_outputs[name]
        stat = storage.stat(entry.key)
        if stat is None:
            raise RuntimeError(f"Atomic A2V B2 sidecar missing: {name}")
        files[name] = (
            entry.key, a.output_dir / f"batch2-{'b' if name == 'video_b' else 'report'}.{ext}",
            stat.metadata.get("artifact-sha256"),
        )
    for name, (key, path, sha) in files.items():
        storage.download(key, path)
        if not sha or sha256_file(path) != sha:
            raise RuntimeError(f"R2 integrity mismatch for A2V B2 {name}")
    report = json.loads((a.output_dir / "batch2-report.json").read_text())
    if report.get("actual_gpu_batch_size") != 2 or report.get("mode") != "batch2":
        raise RuntimeError("A2V B2 backend did not perform real paired GPU forwards")
    expected_backend = (
        "triton" if a.vae_backend == "triton_experimental" else "eager"
    )
    if report.get("guidance_forward_batch_size") != a.guidance_forward_batch_size:
        raise RuntimeError("A2V reported a different guidance forward batch")
    if report.get("max_stage1_major_page_faults") != a.max_stage1_major_page_faults:
        raise RuntimeError("A2V reported a different host paging guard")
    for key in (
        "progressive_block_group_size",
        "min_checkpoint_direct_read_mibps",
        "max_stage1_build_seconds",
    ):
        if report.get(key) != getattr(a, key):
            raise RuntimeError(f"A2V did not execute the requested optimization: {key}")
    if report.get("video_vae_backend") != expected_backend:
        raise RuntimeError("A2V reported a different decoder backend: silent fallback")
    report["technical_validation"] = {}
    for label, source in (("a", a.audio_a), ("b", a.audio_b)):
        video = a.output_dir / f"batch2-{label}.mp4"
        report["technical_validation"][label] = _technical_video(
            video, fps=24, num_frames=provenance["num_frames"],
        )
        _validate_reference_voice_tail(source, video)
    report["audio_tail_validation"] = "PASSED_BOTH_OUTPUTS"
    report["lipsync_status"] = "B1_FRAME_BY_FRAME_HUMAN_REVIEW_REQUIRED"
    if a.reference_a is not None:
        from compare_ltx25_a2v_b2_vs_b1 import compare

        comparison = compare(
            audio_a=a.audio_a, audio_b=a.audio_b,
            baseline_a=a.reference_a, baseline_b=a.reference_b,
            candidate_a=a.output_dir / "batch2-a.mp4",
            candidate_b=a.output_dir / "batch2-b.mp4",
            destination=a.output_dir,
        )
        if a.vae_backend == "triton_experimental":
            report["eager_backend_comparison"] = comparison
            report["promotion_status"] = (
                "HUMAN_COMBINED_B4_TRITON_APPROVAL_REQUIRED"
                if a.guidance_forward_batch_size == 4
                else "HUMAN_EAGER_TRITON_VISUAL_LIPSYNC_APPROVAL_REQUIRED"
            )
        elif a.guidance_forward_batch_size == 4:
            report["split_b2_guidance_comparison"] = comparison
            report["promotion_status"] = "HUMAN_SPLIT2_FUSED4_VISUAL_LIPSYNC_APPROVAL_REQUIRED"
        else:
            report["b1_comparison"] = comparison
    if a.performance_target_seconds:
        report["performance_target_seconds"] = a.performance_target_seconds
        report["performance_target_met"] = (
            report["elapsed_seconds"] < a.performance_target_seconds
        )
        report["performance_target_status"] = (
            "MET" if report["performance_target_met"] else "MISSED"
        )
        # Never discard paid videos on a performance miss: we need the
        # complete phase trace and frame comparison to investigate safely.
    (a.output_dir / "experiment-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    print("Output directory: " + str(a.output_dir))
    if a.performance_target_seconds:
        print("A2V_B2_PERFORMANCE_TARGET_" + report["performance_target_status"])


if __name__ == "__main__":
    main()
