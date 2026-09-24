from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

from PIL import Image

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.storage import R2ObjectStorage, sha256_file
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workers.qwen_image_21 import (
    QWEN_IMAGE_21_DEFAULT_STEPS,
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_MODEL_ID,
    QWEN_IMAGE_21_MODEL_REVISION,
    QWEN_IMAGE_21_TRUE_CFG_SCALE,
    QWEN_IMAGE_21_USE_KV_CACHE,
)
from ai_video_factory.workers.qwen_image_21.model import QWEN_IMAGE_21_BENCHMARK_PROFILE

WIDTH, HEIGHT = 1536, 864


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def _ready_instance(
    *, api_key: str, organization: str, project: str, group_name: str
) -> tuple[str, str]:
    url = (
        f"https://api.salad.com/api/public/organizations/{organization}/projects/"
        f"{project}/containers/{group_name}/instances"
    )
    request = urllib.request.Request(
        url, headers={"Salad-Api-Key": api_key, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    instances = payload.get("instances", payload.get("items", []))
    ready = [
        instance for instance in instances if instance.get("started") and instance.get("ready")
    ]
    if len(ready) != 1:
        raise RuntimeError(
            f"Expected exactly one started/ready Qwen instance, got {len(ready)}"
        )
    instance = ready[0]
    identifier = str(instance.get("id") or "")
    if not identifier:
        raise RuntimeError("Salad ready instance has no identifier")
    return identifier, str(instance.get("machine_id") or "")


def _make_request(*, job_id: str, prompt: str, seed: int) -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id=job_id,
        task=QWEN_IMAGE_21_KEYFRAME_TASK,
        output=ObjectOutput(key=f"jobs/{job_id}/image.png", content_type="image/png"),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json", content_type="application/json"
            )
        },
        max_attempts=1,
        parameters={
            "generation_profile": QWEN_IMAGE_21_BENCHMARK_PROFILE,
            "model_id": QWEN_IMAGE_21_MODEL_ID,
            "model_revision": QWEN_IMAGE_21_MODEL_REVISION,
            "prompt": prompt,
            "width": WIDTH,
            "height": HEIGHT,
            "seed": seed,
            "num_inference_steps": QWEN_IMAGE_21_DEFAULT_STEPS,
            "true_cfg_scale": QWEN_IMAGE_21_TRUE_CFG_SCALE,
            "use_kv_cache": QWEN_IMAGE_21_USE_KV_CACHE,
        },
    )


def _validate_metrics(
    metrics: dict[str, object],
    *,
    seed: int,
    worker_id: str | None,
    generation: int,
) -> str:
    expected = {
        "generation_profile": QWEN_IMAGE_21_BENCHMARK_PROFILE,
        "model_revision": QWEN_IMAGE_21_MODEL_REVISION,
        "width": WIDTH,
        "height": HEIGHT,
        "num_inference_steps": QWEN_IMAGE_21_DEFAULT_STEPS,
        "seed": seed,
        "memory_mode": "int8_cuda",
    }
    for key, value in expected.items():
        if metrics.get(key) != value:
            raise RuntimeError(
                f"Qwen generation {generation}: unexpected {key}={metrics.get(key)!r}"
            )
    identity = str(metrics.get("worker_id") or "")
    if not identity:
        raise RuntimeError("Qwen metadata did not identify the container process")
    if worker_id is not None and identity != worker_id:
        raise RuntimeError("Qwen worker process changed between benchmark generations")
    if generation > 1 and metrics.get("pipeline_reused") is not True:
        raise RuntimeError("Qwen warm generation rebuilt its model pipeline")
    for name in ("inference_seconds", "png_save_seconds", "total_elapsed_seconds"):
        if float(metrics[name]) <= 0:
            raise RuntimeError(f"Qwen generation {generation} has invalid {name}")
    if int(metrics["peak_vram_allocated_bytes"]) <= 0:
        raise RuntimeError("Qwen worker did not report CUDA peak VRAM")
    return identity


def _write_summary(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark five real, uncached Qwen INT8 jobs.")
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=4242)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0 <= args.seed <= 2_147_483_647:
        raise ValueError("Seed must be between 0 and 2147483647")
    prompt = args.prompt_file.read_text(encoding="utf-8-sig").strip()
    if not prompt:
        raise ValueError("Benchmark prompt file is empty")
    manifest = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    stack = manifest["stack"]
    service = manifest["services"]["qwen_image_21"]
    organization = _required("SALAD_ORGANIZATION")
    project = _required("SALAD_PROJECT")
    if (organization, project) != (stack["organization"], stack["project"]):
        raise RuntimeError("Salad credentials do not match the deployment manifest")
    api_key = _required("SALAD_API_KEY")
    if service["environment"]["QWEN_IMAGE_21_MEMORY_MODE"] != "int8_cuda":
        raise RuntimeError("Qwen benchmark requires the manifest INT8 CUDA profile")
    storage = R2ObjectStorage.create(
        endpoint_url=_required("R2_ENDPOINT_URL"),
        bucket=_required("R2_BUCKET"),
        access_key_id=_required("R2_ACCESS_KEY_ID"),
        secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
    )
    queue = SaladJobQueueClient(
        organization=organization,
        project=project,
        queue_name=service["queue_name"],
        api_key=api_key,
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=5,
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=180,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = args.output_dir / "benchmark-summary.json"
    group_name = service["group_name"]
    instance_id, machine_id = _ready_instance(
        api_key=api_key, organization=organization, project=project, group_name=group_name
    )
    worker_id: str | None = None
    runs: list[dict[str, object]] = []
    summary: dict[str, object] = {
        "profile": QWEN_IMAGE_21_BENCHMARK_PROFILE,
        "image": service["image"],
        "prompt": prompt,
        "seed": args.seed,
        "width": WIDTH,
        "height": HEIGHT,
        "steps": QWEN_IMAGE_21_DEFAULT_STEPS,
        "instance_id": instance_id,
        "machine_id": machine_id,
        "runs": runs,
        "quality_review": "pending_manual_png_review",
        "worker_stopped": None,
        "run_error": None,
    }
    try:
        for generation in range(1, 6):
            current_id, current_machine = _ready_instance(
                api_key=api_key,
                organization=organization,
                project=project,
                group_name=group_name,
            )
            if (current_id, current_machine) != (instance_id, machine_id):
                raise RuntimeError("Qwen Salad instance changed before a benchmark generation")
            job_id = f"qwen-benchmark-{uuid4().hex}-{generation:02d}"
            request = _make_request(job_id=job_id, prompt=prompt, seed=args.seed)
            directory = args.output_dir / f"run-{generation:02d}"
            directory.mkdir(parents=True, exist_ok=True)
            print(f"Qwen generation {generation}/5: job_id={job_id}", flush=True)
            submitted = time.monotonic()
            response = executor.execute(
                request,
                metadata={"capability": "qwen-image-21-benchmark", "generation": str(generation)},
            )
            if response.replayed or response.attempt_count != 1:
                raise RuntimeError("Qwen benchmark received a cached or retried result")
            output = directory / "image.png"
            executor.download_output(response, output)
            if request.sidecar_outputs is None:
                raise RuntimeError("Benchmark request omitted its required metrics sidecar")
            metadata_key = request.sidecar_outputs["metadata"].key
            stored = storage.stat(metadata_key)
            if stored is None:
                raise RuntimeError("Qwen benchmark did not upload its metrics sidecar")
            metadata_path = directory / "metadata.json"
            storage.download(metadata_key, metadata_path)
            if sha256_file(metadata_path) != stored.metadata.get("artifact-sha256"):
                raise RuntimeError("Qwen metrics sidecar failed its SHA-256 verification")
            if stored.metadata.get("request-sha256") != request.fingerprint():
                raise RuntimeError("Qwen metrics sidecar belongs to a different request")
            metrics = json.loads(metadata_path.read_text(encoding="utf-8"))
            worker_id = _validate_metrics(
                metrics, seed=args.seed, worker_id=worker_id, generation=generation
            )
            with Image.open(output) as image:
                image.load()
                if image.format != "PNG" or image.size != (WIDTH, HEIGHT):
                    raise RuntimeError("Qwen benchmark produced an invalid PNG geometry or format")
            current_id, current_machine = _ready_instance(
                api_key=api_key,
                organization=organization,
                project=project,
                group_name=group_name,
            )
            if (current_id, current_machine) != (instance_id, machine_id):
                raise RuntimeError("Qwen Salad instance changed during a benchmark generation")
            row: dict[str, object] = {
                "generation": generation,
                "warmup": generation == 1,
                "job_id": job_id,
                "inference_seconds": metrics["inference_seconds"],
                "png_save_seconds": metrics["png_save_seconds"],
                "total_seconds": metrics["total_elapsed_seconds"],
                "peak_vram_allocated_bytes": metrics["peak_vram_allocated_bytes"],
                "peak_vram_reserved_bytes": metrics["peak_vram_reserved_bytes"],
                "pipeline_reused": metrics["pipeline_reused"],
                "worker_id": worker_id,
                "instance_id": instance_id,
                "job_wall_seconds": round(time.monotonic() - submitted, 3),
                "png": str(output),
            }
            runs.append(row)
            _write_summary(report, summary)
        def describe(values: list[float]) -> dict[str, float]:
            return {
                "median_seconds": statistics.median(values),
                "mean_seconds": statistics.mean(values),
                "min_seconds": min(values),
                "max_seconds": max(values),
                "range_seconds": max(values) - min(values),
            }

        warm_total = describe([float(row["total_seconds"]) for row in runs[1:]])
        warm_inference = describe([float(row["inference_seconds"]) for row in runs[1:]])
        warm_png_save = describe([float(row["png_save_seconds"]) for row in runs[1:]])
        median = warm_total["median_seconds"]
        high_variation = warm_total["range_seconds"] > 0.20 * median
        category = (
            "investigate"
            if median > 35 or high_variation
            else "objective_met"
            if median <= 30
            else "initially_acceptable"
        )
        summary["warm_total"] = {
            **warm_total,
            "high_variation_over_20_percent": high_variation,
            "category": category,
        }
        summary["warm_inference"] = warm_inference
        summary["warm_png_save"] = warm_png_save
        print(
            f"Qwen warm total median={median:.3f}s "
            f"mean={warm_total['mean_seconds']:.3f}s category={category}"
        )
    except Exception as exc:
        summary["run_error"] = str(exc)
        raise
    finally:
        _write_summary(report, summary)
        print(f"Qwen benchmark report: {report}", flush=True)


if __name__ == "__main__":
    main()
