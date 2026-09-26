"""Official GPT Image Batch API: one JSONL line and one batch per B2 image.

A generation POST is recorded before transmission. Unknown batch creation and
unknown direct generation outcomes are never automatically repeated.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image

from .openai_images import OpenAIImageClient

_BASE = "https://api.openai.com/v1"
_IMAGE_ENDPOINT = "/v1/images/generations"
_IMAGE_PREFIX = "iphone 6 photo done by an elderly:  "
_MAX_PNG = 30 * 1024 * 1024
_MAX_OUTPUT = (_MAX_PNG * 4 // 3) + 1024 * 1024
_BEAT_ID = re.compile(r"[1-9][0-9]*[A-Z]+", flags=re.ASCII)
_API_ID = re.compile(r"[A-Za-z0-9_-]+", flags=re.ASCII)
_TERMINAL_FAILURE = {"failed", "expired", "cancelled", "canceled"}
_PENDING = {"validating", "in_progress", "finalizing", "cancelling"}


class ImageBatchError(RuntimeError):
    """The image is incomplete or needs a safe, explicit recovery."""


class ImageBatchTimeout(ImageBatchError):
    """The persisted 30-minute per-image deadline has elapsed."""


@dataclass(frozen=True)
class ImageBatchOptions:
    model_id: str = "gpt-image-2.5-flare"
    size: str = "1280x720"
    quality: str = "low"
    timeout_seconds: int = 1800
    poll_interval_seconds: float = 8.0
    max_parallel_images: int = 4

    def __post_init__(self) -> None:
        if self.model_id not in {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}:
            raise ValueError("Unsupported official GPT Image model")
        if self.size != "1280x720" or self.quality != "low":
            raise ValueError("B2 images must use 1280x720 and low quality")
        if self.timeout_seconds < 1 or self.poll_interval_seconds <= 0:
            raise ValueError("Batch timeout and polling interval must be positive")
        if not 1 <= self.max_parallel_images <= 16:
            raise ValueError("max_parallel_images must be between 1 and 16")

    def request(self, description: str) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "prompt": effective_image_prompt(description),
            "size": self.size,
            "quality": self.quality,
            "output_format": "png",
            "n": 1,
        }


def effective_image_prompt(description: str) -> str:
    """Prefix only the outgoing request; never rewrite B2's original description."""
    return _IMAGE_PREFIX + description


def described_beats(plan: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = plan.get("blocks")
    if not isinstance(blocks, list):
        raise ImageBatchError("B2 visual plan is missing its blocks")
    result: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for block in blocks:
        block_id = block.get("block_id")
        if not isinstance(block_id, int) or isinstance(block_id, bool) or block_id < 1:
            raise ImageBatchError("Invalid B2 block ID")
        beats = block.get("beats")
        if not isinstance(beats, list):
            raise ImageBatchError("Invalid B2 beats")
        for beat in beats:
            description = beat.get("description")
            if description is None or (isinstance(description, str) and not description.strip()):
                continue
            beat_id = beat.get("beat_id")
            if (
                not isinstance(description, str)
                or not isinstance(beat_id, str)
                or not _BEAT_ID.fullmatch(beat_id)
                or not beat_id.startswith(str(block_id))
            ):
                raise ImageBatchError("Invalid described B2 beat")
            key = (block_id, beat_id)
            if key in seen:
                raise ImageBatchError(f"Duplicate described B2 beat: {beat_id}")
            seen.add(key)
            result.append({
                "block_id": block_id,
                "beat_id": beat_id,
                "visual_type": beat.get("visual_type"),
                "description": description,
            })
    return result


def _fingerprint(job: dict[str, Any], options: ImageBatchOptions) -> str:
    identity = {
        "beat_id": job["beat_id"],
        "block_id": job["block_id"],
        **options.request(job["description"]),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ImageBatchError(f"Refusing linked image state: {path}")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ImageBatchError(f"Refusing linked batch input: {path}")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or not _API_ID.fullmatch(value):
        raise ImageBatchError("Invalid OpenAI file or batch identifier")
    return quote(value, safe="")


class OpenAIBatchImageClient:
    """Minimal official HTTP transport, with one JSONL file per image batch."""

    def __init__(self, api_key: str) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for image batches")
        self._api_key = api_key

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            _BASE + path,
            data=data,
            method=method,
            headers=self._headers("application/json" if data is not None else None),
        )
        with urlopen(request, timeout=90) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ImageBatchError("OpenAI returned a non-object JSON response")
        return result

    def upload_jsonl(self, content: bytes, *, filename: str) -> str:
        if len(content) > 1024 * 1024 or not re.fullmatch(r"[a-zA-Z0-9_.-]+", filename):
            raise ImageBatchError("Invalid individual image batch input")
        boundary = "ai-video-factory-" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        request = Request(
            _BASE + "/files",
            data=body,
            method="POST",
            headers=self._headers(f"multipart/form-data; boundary={boundary}"),
        )
        with urlopen(request, timeout=90) as response:
            result = json.load(response)
        file_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(file_id, str):
            raise ImageBatchError("OpenAI file upload returned no file ID")
        return _safe_id(file_id)

    def create_batch(self, file_id: str, *, beat_id: str, fingerprint: str) -> dict[str, Any]:
        return self._request_json(
            "POST", "/batches",
            {
                "input_file_id": _safe_id(file_id),
                "endpoint": _IMAGE_ENDPOINT,
                "completion_window": "24h",
                "metadata": {
                    "b2_beat_id": beat_id,
                    "b2_request_sha256": fingerprint,
                },
            },
        )

    def retrieve_batch(self, batch_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/batches/{_safe_id(batch_id)}")

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        return self._request_json("POST", f"/batches/{_safe_id(batch_id)}/cancel")

    def fetch_file_content(self, file_id: str) -> bytes:
        request = Request(
            _BASE + f"/files/{_safe_id(file_id)}/content",
            method="GET",
            headers=self._headers(),
        )
        with urlopen(request, timeout=180) as response:
            content = response.read(_MAX_OUTPUT + 1)
        if len(content) > _MAX_OUTPUT:
            raise ImageBatchError("Individual batch output exceeds the size limit")
        return content


def _save_batch_png(
    result: dict[str, Any], destination: Path, *, options: ImageBatchOptions
) -> dict[str, Any]:
    images = result.get("data")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ImageBatchError("Batch image response must contain exactly one image")
    encoded = images[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded or len(encoded) > (_MAX_PNG * 4 // 3 + 4):
        raise ImageBatchError("Missing or oversized batch image base64")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageBatchError("Invalid batch image base64") from exc
    if len(data) > _MAX_PNG:
        raise ImageBatchError("Batch PNG exceeds 30 MiB")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG":
                raise ImageBatchError("Batch image is not PNG")
            width, height = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise ImageBatchError("Invalid batch PNG bytes") from exc
    if (width, height) != (1280, 720):
        raise ImageBatchError("Batch PNG dimensions differ from 1280x720")
    temporary = destination.with_name(destination.name + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or temporary.is_symlink():
        raise ImageBatchError("Refusing linked image output")
    try:
        temporary.write_bytes(data)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    usage = result.get("usage")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": width,
        "height": height,
        "size": options.size,
        "output_format": "png",
        "usage": usage if isinstance(usage, dict) else None,
        "created": result.get("created"),
    }


def _one_batch_result(
    batch_client: OpenAIBatchImageClient,
    batch: dict[str, Any],
    *,
    custom_id: str,
    image_path: Path,
    options: ImageBatchOptions,
) -> dict[str, Any]:
    file_id = batch.get("output_file_id")
    if not isinstance(file_id, str):
        raise ImageBatchError("Completed batch has no output_file_id")
    content = batch_client.fetch_file_content(file_id)
    try:
        lines = [line for line in content.decode("utf-8").splitlines() if line.strip()]
        if len(lines) != 1:
            raise ImageBatchError("Each image batch must return exactly one JSONL result")
        row = json.loads(lines[0])
    except (UnicodeError, ValueError) as exc:
        raise ImageBatchError("Invalid individual batch result JSONL") from exc
    if not isinstance(row, dict) or row.get("custom_id") != custom_id:
        raise ImageBatchError("Batch result custom_id does not match the image beat")
    response = row.get("response")
    if (
        row.get("error") is not None
        or not isinstance(response, dict)
        or response.get("status_code") != 200
        or not isinstance(response.get("body"), dict)
    ):
        raise ImageBatchError("OpenAI batch image request failed")
    return _save_batch_png(response["body"], image_path, options=options)


def _mark_complete(
    job: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    image_path: Path,
    output: Path,
    generated: dict[str, Any],
    *,
    provider: str,
    options: ImageBatchOptions,
) -> dict[str, Any]:
    artifact = {
        **job,
        "provider": provider,
        "model_id": options.model_id,
        "size": options.size,
        "quality": options.quality,
        "file": image_path.relative_to(output).as_posix(),
        "batch_id": state.get("batch_id"),
        "input_file_id": state.get("input_file_id"),
        "fallback_reason": state.get("fallback_reason"),
        "batch_may_complete_later": (
            provider == "openai_direct_fallback" and bool(state.get("batch_id"))
        ),
        "sha256": generated["sha256"],
        "width": generated["width"],
        "height": generated["height"],
        "openai_usage": generated.get("usage"),
        "openai_created": generated.get("created"),
        "status": "completed",
    }
    state["artifact"] = artifact
    state["sha256"] = generated["sha256"]
    state["status"] = "completed"
    _atomic_json(state_path, state)
    return artifact


def _direct_fallback(
    job: dict[str, Any],
    options: ImageBatchOptions,
    output: Path,
    state: dict[str, Any],
    state_path: Path,
    image_path: Path,
    *,
    api_key: str | None,
    direct_client: OpenAIImageClient | None,
) -> dict[str, Any]:
    if state.get("status") not in {"batch_timed_out", "batch_failed"}:
        raise ImageBatchError("Direct generation requires a timed-out or failed batch")
    if direct_client is None:
        if not api_key:
            raise ImageBatchError(
                f"OPENAI_API_KEY is missing for direct fallback of {job['beat_id']}; "
                "resume with --images-only and the original saved batch"
            )
        direct_client = OpenAIImageClient(api_key)
    state["direct_started_at_unix"] = time.time()
    state["status"] = "direct_submitting_unknown"
    _atomic_json(state_path, state)
    try:
        result = direct_client.generate_png(
            prompt=effective_image_prompt(job["description"]),
            model=options.model_id,
            size=options.size,
            quality=options.quality,
            output_format="png",
            destination=image_path,
        )
    except Exception as exc:
        raise ImageBatchError(
            f"Direct image POST outcome for {job['beat_id']} is unknown. "
            f"Inspect {state_path}; never resubmit automatically."
        ) from exc
    return _mark_complete(
        job, state, state_path, image_path, output, result,
        provider="openai_direct_fallback", options=options,
    )


def _wait_for_batch(
    batch_client: OpenAIBatchImageClient,
    state: dict[str, Any],
    *,
    options: ImageBatchOptions,
) -> dict[str, Any]:
    batch_id = state["batch_id"]
    submitted_at = state.get("submitted_at_unix")
    if not isinstance(submitted_at, (int, float)):
        raise ImageBatchError("Missing original batch submission timestamp")
    deadline = float(submitted_at) + options.timeout_seconds
    while True:
        try:
            batch = batch_client.retrieve_batch(batch_id)
        except (HTTPError, URLError, TimeoutError):
            if time.time() >= deadline:
                raise ImageBatchTimeout(f"Batch {batch_id} exceeded its 30-minute deadline")
            time.sleep(min(options.poll_interval_seconds, max(0, deadline - time.time())))
            continue
        if batch.get("id") != batch_id or batch.get("input_file_id") != state["input_file_id"]:
            raise ImageBatchError("Batch response does not match the saved request")
        status = batch.get("status")
        if status == "completed" or status in _TERMINAL_FAILURE:
            return batch
        if status not in _PENDING:
            raise ImageBatchError(f"Unrecognized OpenAI batch status: {status}")
        if time.time() >= deadline:
            raise ImageBatchTimeout(f"Batch {batch_id} exceeded its 30-minute deadline")
        time.sleep(min(options.poll_interval_seconds, max(0, deadline - time.time())))


def _one_image(
    job: dict[str, Any],
    output: Path,
    options: ImageBatchOptions,
    *,
    api_key: str | None,
    batch_client: OpenAIBatchImageClient,
    direct_client: OpenAIImageClient | None,
) -> dict[str, Any]:
    folder = output / "images" / f"block_{job['block_id']}"
    state_path = folder / f"{job['beat_id']}.json"
    image_path = folder / f"{job['beat_id']}.png"
    batch_path = folder / f"{job['beat_id']}.batch.jsonl"
    if any(path.is_symlink() for path in (folder, state_path, image_path, batch_path)):
        raise ImageBatchError(f"Refusing linked image assets for {job['beat_id']}")
    fingerprint = _fingerprint(job, options)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("schema_version") != "openai-image-batch-v1":
            raise ImageBatchError(
                f"Existing legacy image state for {job['beat_id']} must be reconciled "
                "before switching providers; use a separate output root."
            )
        if state.get("fingerprint") != fingerprint:
            raise ImageBatchError(
                f"B2 prompt changed for {job['beat_id']}; use a separate output root."
            )
    else:
        state = {
            **job,
            "schema_version": "openai-image-batch-v1",
            "fingerprint": fingerprint,
            "model_id": options.model_id,
            "status": "upload_pending",
        }
        _atomic_json(state_path, state)

    if state["status"] == "completed":
        if image_path.is_file() and hashlib.sha256(image_path.read_bytes()).hexdigest() == state.get("sha256"):
            return state["artifact"]
        raise ImageBatchError(
            f"Completed PNG for {job['beat_id']} is missing or changed; "
            "do not automatically repeat a paid request."
        )
    if state["status"] in {"batch_creating_unknown", "direct_submitting_unknown"}:
        raise ImageBatchError(
            f"Paid request for {job['beat_id']} has an unknown outcome. "
            f"Reconcile {state_path} with OpenAI before any further generation."
        )
    if state["status"] in {"batch_timed_out", "batch_failed"}:
        return _direct_fallback(
            job, options, output, state, state_path, image_path,
            api_key=api_key, direct_client=direct_client,
        )

    if state["status"] == "upload_pending":
        custom_id = f"b2-{job['beat_id']}-{fingerprint[:12]}"
        request = {
            "custom_id": custom_id,
            "method": "POST",
            "url": _IMAGE_ENDPOINT,
            "body": options.request(job["description"]),
        }
        jsonl = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
        _atomic_text(batch_path, jsonl)
        state["custom_id"] = custom_id
        _atomic_json(state_path, state)
        # Uploading a second *file* after an uncertain upload cannot submit
        # another image; only the subsequent /batches POST starts processing.
        try:
            file_id = batch_client.upload_jsonl(jsonl.encode("utf-8"), filename=f"{job['beat_id']}.jsonl")
        except Exception as exc:
            raise ImageBatchError(
                f"Input file upload failed for {job['beat_id']}; --images-only may retry the upload."
            ) from exc
        state["input_file_id"] = file_id
        state["status"] = "batch_ready"
        _atomic_json(state_path, state)

    if state["status"] == "batch_ready":
        state["submitted_at_unix"] = time.time()
        state["status"] = "batch_creating_unknown"
        _atomic_json(state_path, state)
        try:
            batch = batch_client.create_batch(
                state["input_file_id"], beat_id=job["beat_id"], fingerprint=fingerprint
            )
        except Exception as exc:
            raise ImageBatchError(
                f"Batch creation outcome for {job['beat_id']} is unknown. "
                "Do not create another batch or start direct generation automatically."
            ) from exc
        batch_id = batch.get("id")
        if not isinstance(batch_id, str) or not _API_ID.fullmatch(batch_id):
            raise ImageBatchError(
                f"OpenAI returned no usable batch ID for {job['beat_id']}; "
                "do not submit again."
            )
        if batch.get("input_file_id") != state["input_file_id"] or batch.get("endpoint") != _IMAGE_ENDPOINT:
            raise ImageBatchError("Created batch does not match the saved input file or endpoint")
        state["batch_id"] = batch_id
        state["batch_status"] = batch.get("status")
        state["status"] = "batch_submitted"
        _atomic_json(state_path, state)

    if state["status"] != "batch_submitted":
        raise ImageBatchError(f"Unrecognized image state for {job['beat_id']}")
    try:
        batch = _wait_for_batch(batch_client, state, options=options)
    except ImageBatchTimeout:
        state["status"] = "batch_timed_out"
        state["fallback_reason"] = "batch_timeout"
        state["batch_timeout_at_unix"] = time.time()
        _atomic_json(state_path, state)
        # Cancellation is best effort: it can take time and does not guarantee
        # the batch cannot complete or incur a charge before direct fallback.
        try:
            cancelled = batch_client.cancel_batch(state["batch_id"])
            state["batch_cancel_status"] = cancelled.get("status")
        except Exception as exc:
            state["batch_cancel_error"] = type(exc).__name__
        _atomic_json(state_path, state)
        return _direct_fallback(
            job, options, output, state, state_path, image_path,
            api_key=api_key, direct_client=direct_client,
        )
    state["batch_status"] = batch["status"]
    state["output_file_id"] = batch.get("output_file_id")
    _atomic_json(state_path, state)
    if batch["status"] == "completed":
        try:
            generated = _one_batch_result(
                batch_client, batch, custom_id=state["custom_id"],
                image_path=image_path, options=options,
            )
        except (ImageBatchError, HTTPError, URLError, TimeoutError) as exc:
            # Completion is known but its sole image is unusable. Do not submit
            # another batch; save the reason and use one ordinary image POST.
            state["status"] = "batch_failed"
            state["fallback_reason"] = "batch_result_unusable"
            state["batch_result_error"] = str(exc)
            _atomic_json(state_path, state)
        else:
            return _mark_complete(
                job, state, state_path, image_path, output, generated,
                provider="openai_batch", options=options,
            )
    else:
        state["status"] = "batch_failed"
        state["fallback_reason"] = "batch_" + str(batch["status"])
        _atomic_json(state_path, state)
    return _direct_fallback(
        job, options, output, state, state_path, image_path,
        api_key=api_key, direct_client=direct_client,
    )


def generate_b2_images(
    output: Path,
    plan: dict[str, Any],
    *,
    api_key: str | None,
    options: ImageBatchOptions,
    batch_client: OpenAIBatchImageClient | None = None,
    direct_client: OpenAIImageClient | None = None,
) -> dict[str, Any]:
    """Use N independent single-line batches for N described beats."""
    jobs = described_beats(plan)
    for job in jobs:
        state_path = output / "images" / f"block_{job['block_id']}" / f"{job['beat_id']}.json"
        if state_path.is_symlink():
            raise ImageBatchError("Refusing linked image state")
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("schema_version") != "openai-image-batch-v1":
                raise ImageBatchError(
                    f"Existing AI33/legacy state at {state_path}: reconcile old paid "
                    "tasks and use a separate output root before making new image batches."
                )
            if state.get("fingerprint") != _fingerprint(job, options):
                raise ImageBatchError(
                    f"Image request differs from saved beat {job['beat_id']}; use another output root."
                )
    if jobs and batch_client is None:
        if not api_key:
            raise ImageBatchError("OPENAI_API_KEY is required for official GPT image batches")
        batch_client = OpenAIBatchImageClient(api_key)
    manifest_path = output / "images" / "manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "openai-b2-image-batches-v1",
        "provider": "openai",
        "model_id": options.model_id,
        "size": options.size,
        "quality": options.quality,
        "batch_completion_window": "24h",
        "batch_timeout_seconds": options.timeout_seconds,
        "max_parallel_images": options.max_parallel_images,
        "status": "running",
        "total_described_beats": len(jobs),
        "openai_batch_count": 0,
        "openai_direct_fallback_count": 0,
        "items": [],
    }
    _atomic_json(manifest_path, manifest)
    results: list[dict[str, Any] | None] = [None] * len(jobs)
    failures: list[tuple[str, Exception]] = []
    try:
        if jobs:
            if batch_client is None:
                raise ImageBatchError("Missing batch client")
            with ThreadPoolExecutor(max_workers=min(options.max_parallel_images, len(jobs))) as pool:
                futures = {
                    pool.submit(
                        _one_image, job, output, options, api_key=api_key,
                        batch_client=batch_client, direct_client=direct_client,
                    ): (i, job["beat_id"])
                    for i, job in enumerate(jobs)
                }
                for future in as_completed(futures):
                    index, beat_id = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:
                        failures.append((beat_id, exc))
                        print(f"  Image {beat_id}: {exc}", flush=True)
                    manifest["items"] = [item for item in results if item is not None]
                    manifest["openai_batch_count"] = sum(
                        item["provider"] == "openai_batch" for item in manifest["items"]
                    )
                    manifest["openai_direct_fallback_count"] = sum(
                        item["provider"] == "openai_direct_fallback" for item in manifest["items"]
                    )
                    _atomic_json(manifest_path, manifest)
        if failures:
            detail = "; ".join(f"{beat}: {exc}" for beat, exc in failures)
            raise ImageBatchError(
                f"{len(failures)} of {len(jobs)} image beats failed; completed beats were saved: {detail}"
            ) from failures[0][1]
        manifest["status"] = "completed"
        _atomic_json(manifest_path, manifest)
    except Exception:
        manifest["status"] = "incomplete"
        _atomic_json(manifest_path, manifest)
        raise
    return manifest


def has_pending_image_tasks(output: Path) -> bool:
    """Protect any durable image request, including old AI33 state, before B1 reruns."""
    for state_path in (output / "images").glob("block_*/*.json"):
        if state_path.is_symlink():
            return True
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") != "completed":
            return True
        if not state_path.with_suffix(".png").is_file():
            return True
        if state.get("artifact", {}).get("batch_may_complete_later"):
            return True
    return False
