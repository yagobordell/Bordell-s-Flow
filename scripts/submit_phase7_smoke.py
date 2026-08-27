from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ai_video_factory.gpu.contracts import GPUJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.gpu.storage import R2ObjectStorage, sha256_file

REQUIRED_ENV = (
    "SALAD_API_KEY",
    "SALAD_ORGANIZATION",
    "SALAD_PROJECT",
    "R2_ENDPOINT_URL",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def _environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _salad_request(
    url: str,
    api_key: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Salad-Api-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ai-video-factory-phase7/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Salad API returned HTTP {exc.code}: {detail}") from exc


def _normalized_queue_output(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"queue output is not JSON: {value!r}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"queue output has an unexpected type: {type(value).__name__}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload, submit, poll and verify a Phase 7 Salad/R2 smoke job."
    )
    parser.add_argument(
        "--queue-name",
        default=os.getenv("SALAD_QUEUE_NAME", "ai-video-factory-jobs"),
    )
    parser.add_argument("--job-id", default=f"phase7-smoke-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--output-dir", type=Path, default=Path("data/output/phase7/salad"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    environment = _environment()
    storage = R2ObjectStorage.create(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )

    with tempfile.TemporaryDirectory(prefix="phase7-submit-") as temporary:
        temporary_dir = Path(temporary)
        source = temporary_dir / "input.txt"
        if args.input is None:
            source.write_text(
                f"AI Video Factory Phase 7 smoke job {args.job_id}\n",
                encoding="utf-8",
            )
        else:
            source.write_bytes(args.input.read_bytes())
        input_sha256 = sha256_file(source)
        input_key = f"phase7/smoke-inputs/{args.job_id}/input{source.suffix or '.bin'}"
        output_key = f"jobs/{args.job_id}/output{source.suffix or '.bin'}"
        content_type = "text/plain" if source.suffix == ".txt" else "application/octet-stream"
        storage.upload(
            source,
            input_key,
            content_type=content_type,
            metadata={"purpose": "phase7-smoke", "artifact-sha256": input_sha256},
        )

        job = GPUJobRequest(
            job_id=args.job_id,
            task="infrastructure.copy",
            inputs=[ObjectInput(name="source", key=input_key, sha256=input_sha256)],
            output=ObjectOutput(key=output_key, content_type=content_type),
        )
        body = {
            "input": job.model_dump(mode="json", exclude_none=True),
            "metadata": {"application_job_id": args.job_id, "phase": "7"},
        }
        base_url = (
            "https://api.salad.com/api/public/organizations/"
            f"{environment['SALAD_ORGANIZATION']}/projects/{environment['SALAD_PROJECT']}"
            f"/queues/{args.queue_name}/jobs"
        )
        created = _salad_request(
            base_url,
            environment["SALAD_API_KEY"],
            method="POST",
            body=body,
        )

        args.output_dir.mkdir(parents=True, exist_ok=True)
        request_path = args.output_dir / f"job-request-{args.job_id}.json"
        response_path = args.output_dir / f"queue-response-{args.job_id}.json"
        request_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        response_path.write_text(json.dumps(created, indent=2) + "\n", encoding="utf-8")
        print(f"application_job_id={args.job_id}")
        print(f"salad_job_id={created['id']}")
        print(request_path)
        print(response_path)

        if not args.wait:
            return

        job_url = f"{base_url}/{created['id']}"
        deadline = time.monotonic() + args.timeout_seconds
        current = created
        while current["status"] not in {"succeeded", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"job did not finish within {args.timeout_seconds} seconds")
            time.sleep(args.poll_seconds)
            current = _salad_request(job_url, environment["SALAD_API_KEY"])
            print(f"status={current['status']}")

        response_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        if current["status"] != "succeeded":
            raise RuntimeError(f"Salad job ended with status {current['status']}")

        output = _normalized_queue_output(current.get("output"))
        if output.get("status") != "succeeded":
            raise RuntimeError(f"worker returned a terminal rejection: {output}")
        artifact = output.get("output", {})
        if artifact.get("key") != output_key or artifact.get("sha256") != input_sha256:
            raise RuntimeError(f"worker output metadata does not match the smoke input: {artifact}")

        downloaded = temporary_dir / "verified-output"
        storage.download(output_key, downloaded)
        if downloaded.read_bytes() != source.read_bytes():
            raise RuntimeError("downloaded R2 output does not match the submitted smoke input")
        print("Phase 7 end-to-end smoke test: OK")


if __name__ == "__main__":
    main()
