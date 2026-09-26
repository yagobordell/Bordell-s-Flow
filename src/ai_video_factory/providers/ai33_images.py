"""Resumable AI33 Pro image generation for B2 descriptions.

Every paid submission is recorded *before* the POST. A lost submission response
must be reconciled manually: this module never guesses that a second POST is safe.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from PIL import Image

from .openai_images import OpenAIImageClient, OpenAIImageError

API_BASE = "https://api.openspeaker.ai"
IMAGE_PROMPT_PREFIX = "iphone 6 photo done by an elderly:  "


def effective_image_prompt(description: str) -> str:
    """Keep B2 descriptions immutable and add the requested prefix only at submission."""
    return IMAGE_PROMPT_PREFIX + description
_RETRYABLE_HTTP = {429, 502, 503, 504}
_BEAT_ID = re.compile(r"[1-9][0-9]*[A-Z]+", flags=re.ASCII)


class AI33ImageError(RuntimeError):
    """Image generation failed or requires explicit recovery."""


class AI33ImageTimeout(AI33ImageError):
    """A known paid AI33 task did not complete within the configured deadline."""


@dataclass(frozen=True)
class AI33ImageOptions:
    model_id: str = "gpt-image-2.5-flare"
    aspect_ratio: str = "16:9"
    resolution: str = "1K"
    quality: str = "low"
    poll_timeout_seconds: int = 1800
    poll_interval_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.model_id not in {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}:
            raise ValueError("AI33 image model must be Flare or Sunburst")
        if self.aspect_ratio != "16:9" or self.resolution != "1K":
            raise ValueError("The integrated AI33 profile requires 16:9 and 1K")
        if self.quality != "low":
            raise ValueError("The integrated AI33 profile requires low quality")
        if self.poll_timeout_seconds < 1 or self.poll_interval_seconds <= 0:
            raise ValueError("AI33 polling limits must be positive")

    def request(self, description: str) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "prompt": effective_image_prompt(description),
            "aspect_ratio": self.aspect_ratio,
            "resolution": self.resolution,
            "quality": self.quality,
        }

    def public(self) -> dict[str, object]:
        return {
            "provider": "ai33",
            "model_id": self.model_id,
            "aspect_ratio": self.aspect_ratio,
            "resolution": self.resolution,
            "quality": self.quality,
        }


class AI33ImageClient:
    """HTTP transport with the real OpenSpeaker task routes, without GPU dependencies."""

    def __init__(self, api_key: str, base_url: str = API_BASE) -> None:
        if not api_key.strip():
            raise ValueError("AI33_API_KEY is required")
        if base_url != API_BASE:
            raise ValueError("AI33 API base URL must be the verified OpenSpeaker endpoint")
        self._api_key = api_key
        self._base_url = base_url

    def request_json(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, Any]:
        body = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        request = Request(
            self._base_url + path,
            data=body,
            headers={
                "xi-api-key": self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urlopen(request, timeout=75) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise AI33ImageError("AI33 returned a non-object JSON response")
        return result

    def download_png(self, url: str, destination: Path) -> tuple[str, int, int]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise AI33ImageError("AI33 returned an unsafe image download URL")
        request = Request(url, headers={"Accept": "image/png,*/*"}, method="GET")
        with urlopen(request, timeout=180) as response:
            content = response.read(30 * 1024 * 1024 + 1)
        if len(content) > 30 * 1024 * 1024:
            raise AI33ImageError("AI33 image exceeds the 30 MiB download limit")
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format != "PNG":
                    raise AI33ImageError("AI33 imageUrl did not return a PNG")
                width, height = image.size
                image.verify()
        except (OSError, ValueError) as exc:
            raise AI33ImageError("AI33 returned invalid PNG bytes") from exc

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        if destination.is_symlink() or temporary.is_symlink():
            raise AI33ImageError("Refusing to write a linked image path")
        try:
            temporary.write_bytes(content)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(content).hexdigest(), width, height


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise AI33ImageError(f"Refusing to write a linked image state: {path}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def described_beats(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Use each original B2 description verbatim; avatar beats have no image request."""
    blocks = plan.get("blocks")
    if not isinstance(blocks, list):
        raise AI33ImageError("B2 visual plan is missing its blocks")
    jobs: list[dict[str, Any]] = []
    keys: set[tuple[int, str]] = set()
    for block in blocks:
        block_id = block.get("block_id")
        if not isinstance(block_id, int) or block_id < 1:
            raise AI33ImageError("Invalid B2 block ID for image generation")
        for beat in block.get("beats", []):
            description = beat.get("description")
            if description is None or (
                isinstance(description, str) and not description.strip()
            ):
                continue
            beat_id = beat.get("beat_id")
            if (
                not isinstance(description, str)
                or not isinstance(beat_id, str)
                or not _BEAT_ID.fullmatch(beat_id)
                or not beat_id.startswith(str(block_id))
            ):
                raise AI33ImageError("Invalid described B2 beat")
            key = (block_id, beat_id)
            if key in keys:
                raise AI33ImageError(f"Duplicate described B2 beat: {beat_id}")
            keys.add(key)
            jobs.append(
                {
                    "block_id": block_id,
                    "beat_id": beat_id,
                    "visual_type": beat.get("visual_type"),
                    "description": description,
                }
            )
    return jobs


def _task_id(response: dict[str, Any]) -> str | None:
    nested = response.get("data")
    candidates = [response]
    if isinstance(nested, dict):
        candidates.append(nested)
    for source in candidates:
        for name in ("task_id", "taskId", "id"):
            value = source.get(name)
            if isinstance(value, str) and value:
                return value
    return None


def _check_success(response: dict[str, Any], phase: str) -> None:
    if response.get("success") is False:
        code = response.get("code", "unknown")
        raise AI33ImageError(f"AI33 {phase} rejected the request ({code})")


def _poll(
    client: AI33ImageClient,
    task_id: str,
    options: AI33ImageOptions,
    *,
    submitted_at_unix: float | None = None,
) -> dict[str, Any]:
    """Use the original submission timestamp, including across --images-only runs."""
    elapsed = (
        max(0.0, time.time() - submitted_at_unix)
        if submitted_at_unix is not None
        else 0.0
    )
    deadline = time.monotonic() + max(0.0, options.poll_timeout_seconds - elapsed)
    retries = 0
    path = f"/v1/task/{quote(task_id, safe='')}"

    def check_result(result: dict[str, Any]) -> bool:
        if result.get("code") == "server_busy":
            return False
        _check_success(result, "polling")
        status = str(result.get("status", "")).lower()
        if status == "done":
            return True
        if status in {"failed", "error", "cancelled", "canceled"}:
            raise AI33ImageError(f"AI33 task {task_id} finished with status {status}")
        return False

    while time.monotonic() < deadline:
        try:
            result = client.request_json("GET", path)
        except HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP:
                raise AI33ImageError(
                    f"AI33 task {task_id} polling failed (HTTP {exc.code})"
                ) from exc
            retries += 1
            time.sleep(min(options.poll_interval_seconds * 2 ** min(retries, 3), 30))
            continue
        except (URLError, TimeoutError):
            retries += 1
            time.sleep(min(options.poll_interval_seconds * 2 ** min(retries, 3), 30))
            continue

        if check_result(result):
            return result
        retries = 0
        time.sleep(options.poll_interval_seconds)

    # One final status check avoids paying twice for a result that completed
    # just as the deadline expired. Busy/503 is not evidence of completion.
    try:
        final_result = client.request_json("GET", path)
    except HTTPError as exc:
        if exc.code not in _RETRYABLE_HTTP:
            raise AI33ImageError(
                f"AI33 task {task_id} final poll failed (HTTP {exc.code})"
            ) from exc
    except (URLError, TimeoutError):
        pass
    else:
        if check_result(final_result):
            return final_result

    raise AI33ImageTimeout(
        f"AI33 task {task_id} is still pending after "
        f"{options.poll_timeout_seconds} seconds."
    )


def _fingerprint(job: dict[str, Any], options: AI33ImageOptions) -> str:
    identity = {
        **options.request(job["description"]),
        "beat_id": job["beat_id"],
        "block_id": job["block_id"],
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _openai_fallback(
    job: dict[str, Any],
    options: AI33ImageOptions,
    output: Path,
    state: dict[str, Any],
    state_path: Path,
    image_path: Path,
    *,
    openai_api_key: str | None,
    openai_client: OpenAIImageClient | None,
) -> dict[str, Any]:
    """Never repeat an official POST whose outcome may be unknown."""
    if state.get("status") == "openai_submitting_unknown":
        raise AI33ImageError(
            f"Official OpenAI fallback for beat {job['beat_id']} may already have "
            f"been charged. Inspect {state_path}; do not submit it again."
        )
    if state.get("status") not in {"ai33_timed_out", "ai33_failed"}:
        raise AI33ImageError("Official fallback requires a confirmed AI33 timeout or failure")
    if not state.get("task_id") and state.get("ai33_failure_stage") not in {
        "pricing", "submission_rejected"
    }:
        raise AI33ImageError("Official fallback requires a known AI33 outcome")
    if openai_client is None:
        if not openai_api_key:
            raise AI33ImageError(
                f"Official fallback for beat {job['beat_id']}: OPENAI_API_KEY is missing. "
                "Configure it and use --images-only to continue without another AI33 POST."
            )
        openai_client = OpenAIImageClient(openai_api_key)

    timed_out = state["status"] == "ai33_timed_out"
    state["fallback_request"] = {
        "model": options.model_id,
        "prompt_sha256": hashlib.sha256(
            effective_image_prompt(job["description"]).encode("utf-8")
        ).hexdigest(),
        "size": "1280x720",
        "quality": options.quality,
        "output_format": "png",
    }
    state["fallback_started_at_unix"] = time.time()
    state["status"] = "openai_submitting_unknown"
    _atomic_json(state_path, state)
    try:
        generated = openai_client.generate_png(
            prompt=effective_image_prompt(job["description"]),
            model=options.model_id,
            destination=image_path,
            size="1280x720",
            quality=options.quality,
            output_format="png",
        )
    except (HTTPError, URLError, TimeoutError, OpenAIImageError, ValueError) as exc:
        raise AI33ImageError(
            f"Official OpenAI fallback outcome for beat {job['beat_id']} is unknown. "
            f"Inspect {state_path}; never automatically repeat a paid request."
        ) from exc

    artifact = {
        **job,
        **options.public(),
        "provider": "openai_official_fallback",
        "primary_provider": "ai33",
        "fallback_reason": "ai33_timeout" if timed_out else "ai33_failure",
        "ai33_failure_stage": state.get("ai33_failure_stage"),
        "ai33_timeout_seconds": options.poll_timeout_seconds if timed_out else None,
        "task_id": state.get("task_id"),
        "file": image_path.relative_to(output).as_posix(),
        "sha256": generated["sha256"],
        "width": generated["width"],
        "height": generated["height"],
        "size": generated["size"],
        "output_format": generated["output_format"],
        "credit_cost": None,
        "provider_credit_cost": None,
        "openai_usage": generated.get("usage"),
        "openai_created": generated.get("created"),
        "ai33_may_complete_later": bool(state.get("task_id")),
        "status": "completed",
    }
    state["fallback_result"] = {
        "sha256": generated["sha256"],
        "width": generated["width"],
        "height": generated["height"],
        "usage": generated.get("usage"),
    }
    state["artifact"] = artifact
    state["sha256"] = generated["sha256"]
    state["status"] = "completed"
    _atomic_json(state_path, state)
    return artifact


def _fallback_after_ai33_failure(
    job: dict[str, Any],
    options: AI33ImageOptions,
    output: Path,
    state: dict[str, Any],
    state_path: Path,
    image_path: Path,
    *,
    stage: str,
    error: Exception,
    openai_api_key: str | None,
    openai_client: OpenAIImageClient | None,
) -> dict[str, Any]:
    """Persist a definitive AI33 failure before starting the existing official fallback."""
    state["status"] = "ai33_failed"
    state["ai33_failure_stage"] = stage
    state["ai33_failure_message"] = str(error)
    state["ai33_failure_at_unix"] = time.time()
    _atomic_json(state_path, state)
    return _openai_fallback(
        job, options, output, state, state_path, image_path,
        openai_api_key=openai_api_key, openai_client=openai_client,
    )


def _one_image(
    client: AI33ImageClient,
    job: dict[str, Any],
    options: AI33ImageOptions,
    output: Path,
    *,
    fallback_enabled: bool = False,
    openai_api_key: str | None = None,
    openai_client: OpenAIImageClient | None = None,
) -> dict[str, Any]:
    folder = output / "images" / f"block_{job['block_id']}"
    state_path = folder / f"{job['beat_id']}.json"
    image_path = folder / f"{job['beat_id']}.png"
    fingerprint = _fingerprint(job, options)
    if state_path.is_symlink() or image_path.is_symlink():
        raise AI33ImageError(f"Refusing to follow linked image artifacts for {job['beat_id']}")
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("fingerprint") != fingerprint:
            raise AI33ImageError(
                f"Image request changed for beat {job['beat_id']}; "
                "preserve the old task and use a separate output root for a new request."
            )
    else:
        state = {
            **job,
            **options.public(),
            "fingerprint": fingerprint,
            "status": "pricing",
        }
        _atomic_json(state_path, state)

    if state["status"] == "completed":
        if image_path.is_file():
            actual_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
            if actual_sha == state.get("sha256"):
                return state["artifact"]
        if state.get("artifact", {}).get("provider") == "openai_official_fallback":
            raise AI33ImageError(
                f"Official fallback PNG for beat {job['beat_id']} is missing or modified. "
                "Do not automatically repeat a paid OpenAI request."
            )

    if state["status"] in {"submitting_unknown", "rejected", "openai_submitting_unknown"}:
        raise AI33ImageError(
            f"Paid submission for beat {job['beat_id']} cannot be retried safely. "
            f"Inspect {state_path} and reconcile the task with its provider."
        )
    if state["status"] == "ai33_failed":
        if not fallback_enabled:
            raise AI33ImageError(
                f"Beat {job['beat_id']} failed at AI33; enable the official fallback "
                "or inspect the saved state before resuming."
            )
        return _openai_fallback(
            job, options, output, state, state_path, image_path,
            openai_api_key=openai_api_key, openai_client=openai_client,
        )

    task_id = state.get("task_id")
    if not task_id:
        request = options.request(job["description"])
        try:
            price = client.request_json("POST", "/v1i/task/price", request)
            _check_success(price, "pricing")
        except (HTTPError, URLError, TimeoutError, AI33ImageError) as exc:
            if fallback_enabled:
                return _fallback_after_ai33_failure(
                    job, options, output, state, state_path, image_path,
                    stage="pricing", error=exc, openai_api_key=openai_api_key,
                    openai_client=openai_client,
                )
            raise AI33ImageError(
                f"AI33 price request failed for beat {job['beat_id']}"
            ) from exc
        state["price_response"] = price
        # The persisted timestamp survives process restarts and includes the POST time.
        state["submitted_at_unix"] = time.time()
        state["status"] = "submitting_unknown"
        _atomic_json(state_path, state)
        try:
            created = client.request_json(
                "POST", "/v1i/task/generate-image", request
            )
        except (HTTPError, URLError, TimeoutError) as exc:
            raise AI33ImageError(
                f"AI33 submission outcome unknown for beat {job['beat_id']}. "
                f"Inspect {state_path}; do not submit it again automatically."
            ) from exc
        state["create_response"] = created
        task_id = _task_id(created)
        if created.get("success") is False:
            state["status"] = "rejected"
            _atomic_json(state_path, state)
            if fallback_enabled:
                return _fallback_after_ai33_failure(
                    job, options, output, state, state_path, image_path,
                    stage="submission_rejected",
                    error=AI33ImageError("AI33 explicitly rejected the image task"),
                    openai_api_key=openai_api_key, openai_client=openai_client,
                )
            _check_success(created, "generation")
        if not task_id:
            _atomic_json(state_path, state)
            raise AI33ImageError(
                f"AI33 did not provide a task ID for beat {job['beat_id']}; "
                "do not resubmit without checking the provider's task history."
            )
        state["task_id"] = task_id
        state["status"] = "submitted"
        _atomic_json(state_path, state)

    if state["status"] == "ai33_timed_out" and fallback_enabled:
        return _openai_fallback(
            job,
            options,
            output,
            state,
            state_path,
            image_path,
            openai_api_key=openai_api_key,
            openai_client=openai_client,
        )

    if state["status"] == "download_pending" and isinstance(
        state.get("last_poll"), dict
    ) and state["last_poll"].get("status") == "done":
        result = state["last_poll"]
    else:
        # Legacy checkpoints did not store a submission timestamp. Start their
        # first deadline when they are resumed; do not guess an earlier time.
        if not isinstance(state.get("submitted_at_unix"), (int, float)):
            state["submitted_at_unix"] = time.time()
            _atomic_json(state_path, state)
        try:
            result = _poll(
                client,
                str(task_id),
                options,
                submitted_at_unix=float(state["submitted_at_unix"]),
            )
        except AI33ImageTimeout:
            if not fallback_enabled:
                raise
            state["status"] = "ai33_timed_out"
            state["ai33_timeout_at_unix"] = time.time()
            state["ai33_timeout_seconds"] = options.poll_timeout_seconds
            _atomic_json(state_path, state)
            return _openai_fallback(
                job,
                options,
                output,
                state,
                state_path,
                image_path,
                openai_api_key=openai_api_key,
                openai_client=openai_client,
            )
        except AI33ImageError as exc:
            if not fallback_enabled:
                raise
            return _fallback_after_ai33_failure(
                job, options, output, state, state_path, image_path,
                stage="polling", error=exc, openai_api_key=openai_api_key,
                openai_client=openai_client,
            )

    state["last_poll"] = result
    state["status"] = "download_pending"
    _atomic_json(state_path, state)

    try:
        metadata = result.get("metadata")
        images = metadata.get("result_images") if isinstance(metadata, dict) else None
        if not isinstance(images, list) or not images or not isinstance(images[0], dict):
            raise AI33ImageError(f"AI33 task {task_id} completed without result_images")
        first = images[0]
        url = first.get("imageUrl")
        if not isinstance(url, str):
            raise AI33ImageError(f"AI33 task {task_id} has no downloadable imageUrl")
        sha, width, height = client.download_png(url, image_path)
        if first.get("mimeType") not in (None, "image/png"):
            raise AI33ImageError("AI33 result image MIME type differs from PNG")
        if (
            first.get("width") not in (None, width)
            or first.get("height") not in (None, height)
        ):
            raise AI33ImageError("AI33 image dimensions differ from its task metadata")
    except (HTTPError, URLError, TimeoutError, AI33ImageError) as exc:
        if not fallback_enabled:
            raise AI33ImageError(
                f"AI33 task {task_id} completed, but its PNG was unusable. "
                "Use --images-only to retry downloading the existing result."
            ) from exc
        return _fallback_after_ai33_failure(
            job, options, output, state, state_path, image_path,
            stage="download", error=exc, openai_api_key=openai_api_key,
            openai_client=openai_client,
        )

    artifact = {
        **job,
        **options.public(),
        "task_id": task_id,
        "file": image_path.relative_to(output).as_posix(),
        "sha256": sha,
        "width": width,
        "height": height,
        "credit_cost": result.get("credit_cost"),
        "provider_credit_cost": metadata.get("providerCreditCost"),
        "status": "completed",
    }
    state["artifact"] = artifact
    state["sha256"] = sha
    state["status"] = "completed"
    _atomic_json(state_path, state)
    return artifact




def generate_b2_images(
    output: Path,
    plan: dict[str, Any],
    *,
    api_key: str | None,
    options: AI33ImageOptions,
    client: AI33ImageClient | None = None,
    fallback_enabled: bool = False,
    openai_api_key: str | None = None,
    openai_client: OpenAIImageClient | None = None,
    max_parallel_images: int = 4,
) -> dict[str, Any]:
    """Generate up to four PNGs concurrently, preserving resumable per-beat state."""
    if isinstance(max_parallel_images, bool) or not 1 <= max_parallel_images <= 16:
        raise ValueError("max_parallel_images must be between 1 and 16")
    jobs = described_beats(plan)
    manifest_path = output / "images" / "manifest.json"
    # Validate *all* previous requests before creating a paid task.
    for job in jobs:
        state_path = (
            output / "images" / f"block_{job['block_id']}" / f"{job['beat_id']}.json"
        )
        if state_path.is_symlink():
            raise AI33ImageError("Refusing linked image state")
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("fingerprint") != _fingerprint(job, options):
                raise AI33ImageError(
                    f"Beat {job['beat_id']} differs from its saved AI33 request; "
                    "use a new output root rather than silently regenerating."
                )

    if jobs and client is None:
        if not api_key:
            raise AI33ImageError(
                "AI33_API_KEY is missing. B2 output is saved; "
                "configure .env and use --images-only to continue."
            )
        client = AI33ImageClient(api_key)
    manifest: dict[str, Any] = {
        "schema_version": "ai33-b2-images-v1",
        **options.public(),
        "status": "running",
        "fallback_enabled": fallback_enabled,
        "fallback_size": "1280x720" if fallback_enabled else None,
        "total_described_beats": len(jobs),
        "ai33_count": 0,
        "openai_fallback_count": 0,
        "items": [],
    }
    _atomic_json(manifest_path, manifest)
    results: list[dict[str, Any] | None] = [None] * len(jobs)
    failures: list[tuple[str, Exception]] = []
    try:
        if jobs:
            if client is None:
                raise AI33ImageError("Missing AI33 image client for described B2 beat")
            # Workers only touch their own beat state/PNG. The main thread is the
            # sole writer of the shared manifest and preserves the B2 beat order.
            with ThreadPoolExecutor(max_workers=min(max_parallel_images, len(jobs))) as pool:
                futures = {}
                for index, job in enumerate(jobs):
                    print(
                        f"  Image {index + 1}/{len(jobs)}: {job['beat_id']} "
                        f"(AI33 {options.model_id})",
                        flush=True,
                    )
                    future = pool.submit(
                        _one_image, client, job, options, output,
                        fallback_enabled=fallback_enabled,
                        openai_api_key=openai_api_key,
                        openai_client=openai_client,
                    )
                    futures[future] = (index, job["beat_id"])
                for future in as_completed(futures):
                    index, beat_id = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:
                        failures.append((beat_id, exc))
                        print(f"  Image {beat_id} failed: {exc}", flush=True)
                    manifest["items"] = [item for item in results if item is not None]
                    manifest["ai33_count"] = sum(
                        item["provider"] == "ai33" for item in manifest["items"]
                    )
                    manifest["openai_fallback_count"] = sum(
                        item["provider"] == "openai_official_fallback"
                        for item in manifest["items"]
                    )
                    _atomic_json(manifest_path, manifest)
        if failures:
            details = "; ".join(f"{beat_id}: {exc}" for beat_id, exc in failures)
            raise AI33ImageError(
                f"{len(failures)} of {len(jobs)} image beats failed; "
                f"completed beats remain saved: {details}"
            ) from failures[0][1]
        manifest["status"] = "completed"
        _atomic_json(manifest_path, manifest)
    except Exception:
        manifest["status"] = "incomplete"
        _atomic_json(manifest_path, manifest)
        raise
    return manifest




def has_pending_image_tasks(output: Path) -> bool:
    """Prevent a full B rerun from destroying chargeable, resumable task IDs."""
    for state_path in (output / "images").glob("block_*/*.json"):
        if state_path.is_symlink():
            return True
        state = json.loads(state_path.read_text(encoding="utf-8"))
        status = state.get("status")
        if status == "completed" and not state_path.with_suffix(".png").is_file():
            return True
        if status not in {"completed", "rejected", "pricing"}:
            return True
    return False
