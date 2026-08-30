from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


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


def _load_json_object(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return document


def _normalized_queue_output(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("Queue response did not contain a JSON object output")


def _application_job_id(request_body: dict[str, Any]) -> str:
    input_payload = request_body.get("input")
    if not isinstance(input_payload, dict):
        raise ValueError("Saved request must contain an input object")
    job_id = input_payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("Saved request input must contain a non-empty job_id")
    return job_id


def _validate_replay(
    current: dict[str, Any],
    request_body: dict[str, Any],
    *,
    expected_attempt_count: int,
) -> dict[str, Any]:
    if current.get("status") != "succeeded":
        raise RuntimeError(f"Replay finished with unexpected status: {current.get('status')}")

    output = _normalized_queue_output(current.get("output"))
    application_job_id = _application_job_id(request_body)
    if output.get("status") != "succeeded":
        raise RuntimeError(f"Worker output has unexpected status: {output.get('status')}")
    if output.get("job_id") != application_job_id:
        raise RuntimeError("Worker output job_id does not match the saved request")
    if output.get("replayed") is not True:
        raise RuntimeError("Worker did not report replayed=true")
    if output.get("attempt_count") != expected_attempt_count:
        raise RuntimeError(
            "Replay changed attempt_count: "
            f"expected {expected_attempt_count}, got {output.get('attempt_count')}"
        )

    input_payload = request_body["input"]
    requested_output = input_payload.get("output")
    actual_output = output.get("output")
    inputs = input_payload.get("inputs")
    if not isinstance(requested_output, dict) or not isinstance(actual_output, dict):
        raise RuntimeError("Replay did not return the requested output artifact")
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict):
        raise RuntimeError("Phase 7 infrastructure.copy smoke request must contain one input")
    if actual_output.get("key") != requested_output.get("key"):
        raise RuntimeError("Replay returned a different output key")
    if actual_output.get("content_type") != requested_output.get("content_type"):
        raise RuntimeError("Replay returned a different output content type")
    if actual_output.get("sha256") != inputs[0].get("sha256"):
        raise RuntimeError("Replay output SHA-256 does not match the source input")
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resubmit a saved Phase 7 smoke request and prove idempotent replay."
    )
    parser.add_argument(
        "request",
        type=Path,
        help="Saved job-request-*.json from the first smoke run",
    )
    parser.add_argument("--queue-name", default=os.getenv("SALAD_QUEUE_NAME"))
    parser.add_argument("--expected-attempt-count", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not args.queue_name:
        parser.error("--queue-name or SALAD_QUEUE_NAME is required")
    if args.expected_attempt_count < 1:
        parser.error("--expected-attempt-count must be >= 1")
    if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
        parser.error("timeouts must be positive")
    return args


def main() -> None:
    args = parse_args()
    request_body = _load_json_object(args.request)
    application_job_id = _application_job_id(request_body)
    api_key = _required_environment("SALAD_API_KEY")
    organization = _required_environment("SALAD_ORGANIZATION")
    project = _required_environment("SALAD_PROJECT")
    base_url = (
        "https://api.salad.com/api/public/organizations/"
        f"{organization}/projects/{project}"
    )
    queue_url = f"{base_url}/queues/{args.queue_name}/jobs"

    created = _salad_request(queue_url, api_key, method="POST", body=request_body)
    salad_job_id = created.get("id")
    if not isinstance(salad_job_id, str) or not salad_job_id:
        raise RuntimeError("Salad did not return a queue job id")

    output_dir = args.output_dir or args.request.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / f"queue-replay-result-{application_job_id}.json"

    deadline = time.monotonic() + args.timeout_seconds
    current = created
    previous_status: str | None = None
    while current.get("status") not in TERMINAL_STATUSES:
        status = str(current.get("status", "unknown"))
        if status != previous_status:
            print(f"status={status}")
            previous_status = status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for Salad replay job {salad_job_id}")
        time.sleep(args.poll_seconds)
        current = _salad_request(f"{queue_url}/{salad_job_id}", api_key)

    result_path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output = _validate_replay(
        current,
        request_body,
        expected_attempt_count=args.expected_attempt_count,
    )
    print(f"application_job_id={application_job_id}")
    print(f"salad_replay_job_id={salad_job_id}")
    print(f"attempt_count={output['attempt_count']}")
    print(f"result={result_path}")
    print("Phase 7 replay/idempotency smoke test: OK")


if __name__ == "__main__":
    main()
