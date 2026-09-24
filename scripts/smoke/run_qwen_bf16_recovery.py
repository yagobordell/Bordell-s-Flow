from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from PIL import Image
from psycopg.rows import dict_row

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.storage import R2ObjectStorage, sha256_file
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.workers.qwen_image_21.model import (
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_REFERENCE_TASK,
    QwenImage21Parameters,
    validate_qwen_output_image,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare Qwen BF16 offload against a prior image, preserving prompt and seed."
    )
    parser.add_argument("--source-job-id", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--output-dir", type=Path, default=Path("data/output/qwen-bf16-recovery"))
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing")
    return value


def preflight(dsn: str, source_job_id: str) -> InferenceJobRequest:
    """Read only. Never allocate GPU if Qwen already has work to execute."""
    with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT status, request FROM gpu.jobs WHERE job_id = %s",
            (source_job_id,),
        ).fetchone()
        if row is None or row["status"] != "succeeded":
            raise RuntimeError("The source Qwen job must exist and be succeeded")
        source = InferenceJobRequest.model_validate(row["request"])
        if source.job_id != source_job_id or source.task != QWEN_IMAGE_21_KEYFRAME_TASK:
            raise RuntimeError("Source job must be the successful Qwen production keyframe")
        if source.inputs or source.sidecar_outputs:
            raise RuntimeError("Source job must be the prompt-only production request")
        QwenImage21Parameters.model_validate(source.parameters)

        active = connection.execute(
            """
            SELECT job_id, status FROM gpu.jobs
            WHERE task IN (%s, %s)
              AND status IN ('pending', 'retryable_failed', 'running')
            ORDER BY updated_at DESC LIMIT 10
            """,
            (QWEN_IMAGE_21_KEYFRAME_TASK, QWEN_IMAGE_21_REFERENCE_TASK),
        ).fetchall()
        if active:
            raise RuntimeError(
                "Refusing Qwen GPU allocation while unrelated Qwen jobs are active: "
                + ", ".join(f"{item['job_id']}={item['status']}" for item in active)
            )
    return source


def build_recovery_request(source: InferenceJobRequest, *, job_id: str) -> InferenceJobRequest:
    """Clone the exact prompt, seed and diffusion settings; change only job identity/output."""
    payload = source.model_dump(mode="json", exclude_none=True)
    payload.update(
        job_id=job_id,
        output=ObjectOutput(
            key=f"jobs/{job_id}/image.png", content_type="image/png"
        ).model_dump(mode="json"),
        sidecar_outputs=None,
        max_attempts=1,
    )
    return InferenceJobRequest.model_validate(payload)


def main() -> None:
    args = parse_args()
    load_dotenv(args.env_file, override=False)
    dsn = required_env("POSTGRES_DSN")
    source = preflight(dsn, args.source_job_id)
    params = QwenImage21Parameters.model_validate(source.parameters)
    print(
        f"QWEN_BF16_PREFLIGHT_OK source_job_id={source.job_id} "
        f"size={params.width}x{params.height} steps={params.num_inference_steps} "
        f"seed={params.seed} active_qwen_jobs=0",
        flush=True,
    )
    if args.preflight_only:
        return

    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    storage = R2ObjectStorage.create(
        endpoint_url=required_env("R2_ENDPOINT_URL"),
        bucket=required_env("R2_BUCKET"),
        access_key_id=required_env("R2_ACCESS_KEY_ID"),
        secret_access_key=required_env("R2_SECRET_ACCESS_KEY"),
    )
    job_id = f"qwen-bf16-recovery-{uuid.uuid4().hex}"
    request = build_recovery_request(source, job_id=job_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "last-job-id.txt").write_text(job_id + "\n", encoding="utf-8")
    print(f"QWEN_BF16_NEW_JOB_ID={job_id}", flush=True)

    queue = PostgresJobQueueClient(dsn=dsn)
    started = time.monotonic()
    try:
        executor = InferenceJobExecutor(
            queue=queue,
            storage=storage,
            poll_seconds=2,
            timeout_seconds=args.timeout_seconds,
            pending_timeout_seconds=args.timeout_seconds,
        )
        response = executor.execute(
            request,
            metadata={"provider": "qwen_image_21", "purpose": "bf16_recovery"},
        )
        if response.replayed or response.attempt_count != 1:
            raise RuntimeError("Recovery image must be new inference in exactly one attempt")
        destination = args.output_dir / f"{job_id}.png"
        executor.download_output(response, destination)
        with Image.open(destination) as image:
            image.load()
            if image.format != "PNG":
                raise RuntimeError(f"Recovery output must be PNG; received {image.format}")
            validate_qwen_output_image(image, width=params.width, height=params.height)
            mode = image.mode
        report = {
            "status": "succeeded",
            "source_job_id": source.job_id,
            "job_id": job_id,
            "replayed": response.replayed,
            "seed": params.seed,
            "prompt_sha256": __import__("hashlib").sha256(params.prompt.encode("utf-8")).hexdigest(),
            "width": params.width,
            "height": params.height,
            "steps": params.num_inference_steps,
            "memory_mode": "bf16_offload",
            "image_mode": mode,
            "artifact": str(destination),
            "sha256": sha256_file(destination),
            "wall_seconds": round(time.monotonic() - started, 3),
        }
        report_path = args.output_dir / f"{job_id}.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"QWEN_BF16_IMAGE_VALIDATED={destination.resolve()}", flush=True)
        print(f"QWEN_BF16_REPORT={report_path.resolve()}", flush=True)
    finally:
        queue.close()


if __name__ == "__main__":
    main()
